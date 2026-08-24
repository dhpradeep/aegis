"""Plumbing shared by the OpenAI- and Anthropic-compatible shims: model
resolution, the client-tool MCP bridge, run config, conversation routing into
Sessions, and completion logging."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk import tool as sdk_tool
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.compat.conversation import (
    Turn,
    create_cli_session,
    find_continuation,
    first_user_text,
    remember,
)
from app.core.config import get_settings
from app.db.base import SessionLocal
from app.db.models import CompletionLog, Session, Tenant, Usage
from app.services import ratelimit
from app.services.agent.runtime import RunConfig
from app.services.agent.session_runner import run_session_message
from app.services.models import EFFORT_LEVELS

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

CHAT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch"]
CHAT_PERMISSION_MODE = "default"
CHAT_MAX_TURNS = 30

CLIENT_TOOLS_SERVER = "client"
CLIENT_TOOL_PREFIX = f"mcp__{CLIENT_TOOLS_SERVER}__"

_EFFORT_SYNONYMS = {"minimal": "low"}

_bg_tasks: set[asyncio.Task] = set()
_log = logging.getLogger("app")


@dataclass
class ClientTool:
    name: str
    description: str | None
    schema: dict[str, Any] | None


def resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


async def effective_model(db: AsyncSession, requested: str | None, tenant_id: str) -> str:
    chosen = requested
    if not chosen or chosen == "default":
        tenant = await db.get(Tenant, tenant_id)
        chosen = (tenant.default_model if tenant else None) or get_settings().default_model
    return resolve_model(chosen)


def effective_effort(value: str | None) -> str | None:
    if value is None:
        return None
    value = _EFFORT_SYNONYMS.get(value, value)
    return value if value in EFFORT_LEVELS else None


def client_tool_name(ev: dict) -> str | None:
    name = ev.get("name") or ""
    if not name.startswith(CLIENT_TOOL_PREFIX):
        return None
    return name[len(CLIENT_TOOL_PREFIX):]


def build_client_tools(tools: list[ClientTool]) -> tuple[dict, list[str]]:
    sdk_tools = []
    allowed: list[str] = []
    for t in tools:
        schema = t.schema
        # create_sdk_mcp_server treats the dict as raw JSON Schema only when
        # "type" and "properties" are both present.
        if not isinstance(schema, dict) or "properties" not in schema:
            schema = {"type": "object", "properties": {}}

        async def _handler(args: dict, _name: str = t.name) -> dict:
            return {
                "content": [
                    {"type": "text", "text": "Tool call captured; executed by the client."}
                ]
            }

        sdk_tools.append(sdk_tool(t.name, t.description or t.name, schema)(_handler))
        allowed.append(f"{CLIENT_TOOL_PREFIX}{t.name}")
    server = create_sdk_mcp_server(CLIENT_TOOLS_SERVER, tools=sdk_tools)
    return {CLIENT_TOOLS_SERVER: server}, allowed


def scratch_workspace() -> Path:
    ws = Path(get_settings().workspace_root) / "_chat" / uuid.uuid4().hex
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def build_run_config(
    *,
    prompt: str,
    system_prompt: str | None,
    client_tools: list[ClientTool],
    model: str,
    effort: str | None,
    cwd: str,
    resume: str | None = None,
) -> RunConfig:
    # Client system prompts ride inside the prompt body: subscription-auth
    # requests get rejected (bogus "out of extra usage" 400) when a large
    # system prompt replaces or extends the CLI's own.
    if system_prompt:
        prompt = f"<system_instructions>\n{system_prompt}\n</system_instructions>\n\n{prompt}"
    if client_tools:
        # One assistant turn: the model either answers or requests tool calls;
        # either way control returns to the client. Server-side tools stay off
        # so nothing shadows the client's own.
        mcp_servers, allowed_tools = build_client_tools(client_tools)
        max_turns = 1
        builtin_tools: list | None = []
    else:
        mcp_servers, allowed_tools = {}, CHAT_ALLOWED_TOOLS
        max_turns = CHAT_MAX_TURNS
        builtin_tools = None
    return RunConfig(
        prompt=prompt,
        cwd=cwd,
        system_prompt=None,
        allowed_tools=allowed_tools,
        permission_mode=CHAT_PERMISSION_MODE,
        mcp_servers=mcp_servers,
        model=model,
        max_turns=max_turns,
        resume=resume,
        timeout_s=get_settings().run_timeout_s,
        effort=effort,
        tools=builtin_tools,
    )


def client_profile(request: Request, default: str) -> str:
    ua = request.headers.get("user-agent", "").lower()
    if "claude-cli" in ua:
        return "claude-code"
    if "opencode" in ua:
        return "opencode"
    return default


async def resolve_conversation(
    db: AsyncSession,
    tenant_id: str,
    turns: list[Turn],
    *,
    agentic: bool,
    profile: str,
) -> tuple[Session | None, list[Turn]]:
    """Agentic requests (client tools present) map onto a Session: an existing
    one when the transcript prefix matches, else a new one. Returns the session
    (marked running) and the turns to feed this run."""
    if not agentic:
        return None, turns
    hit = await find_continuation(db, tenant_id, turns)
    if hit is not None:
        session, delta = hit
    else:
        session = await create_cli_session(db, tenant_id, profile, first_user_text(turns))
        delta = turns
    session.status = "running"
    await db.commit()
    return session, delta


async def release_session(db: AsyncSession, session: Session | None) -> None:
    if session is not None:
        session.status = "active"
        await db.commit()


async def run_events(
    runtime,
    cfg: RunConfig,
    session: Session | None,
    *,
    tenant_id: str,
    api_key_id: str,
    display_prompt: str,
    system_prompt: str | None = None,
) -> AsyncIterator[dict]:
    # Caller holds the run gate; whichever path runs releases it.
    if session is None:
        try:
            async for ev in runtime.stream(cfg):
                yield ev
        finally:
            ratelimit.run_gate.release()
        return
    async for ev in run_session_message(
        runtime,
        cfg,
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        session_id=session.id,
        display_prompt=display_prompt,
        system_prompt=system_prompt,
    ):
        yield ev


async def persist_completion(
    db: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: str,
    model: str,
    streamed: bool,
    request_json: str,
    response_text: str,
    result: dict,
) -> None:
    usage = result.get("usage") or {}
    input_tokens = usage.get("input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    cost_usd = result.get("cost_usd") or 0.0
    duration_ms = result.get("duration_ms") or 0
    db.add(
        Usage(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            session_id=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            num_turns=result.get("num_turns") or 0,
        )
    )
    db.add(
        CompletionLog(
            id="cmpl_" + uuid.uuid4().hex,
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            model=model,
            streamed=streamed,
            request_json=request_json,
            response_text=response_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
    )
    await db.commit()


async def _detached(coro: Coroutine) -> None:
    # Detached + shielded: a client disconnect cancels the streaming generator,
    # and a DB write interrupted mid-flight would poison the pooled connection.
    task = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        pass


async def _persist_streamed(**kw: Any) -> None:
    try:
        async with SessionLocal() as db:
            await persist_completion(db, streamed=True, **kw)
    except Exception:
        _log.exception("failed to persist streamed completion")


async def _remember_streamed(session_id: str, turns: list[Turn]) -> None:
    try:
        async with SessionLocal() as db:
            await remember(db, session_id, turns)
    except Exception:
        _log.exception("failed to record conversation state")


async def after_stream(
    *,
    session: Session | None,
    turns: list[Turn],
    final: dict | None,
    tenant_id: str,
    api_key_id: str,
    model: str,
    request_json: str,
    response_text: str,
) -> None:
    if final is None or final.get("type") != "result":
        return
    if session is not None:
        await _detached(_remember_streamed(session.id, turns))
        return
    await _detached(
        _persist_streamed(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            model=model,
            request_json=request_json,
            response_text=response_text,
            result=final,
        )
    )


async def after_blocking(
    db: AsyncSession,
    *,
    session: Session | None,
    turns: list[Turn],
    final: dict,
    tenant_id: str,
    api_key_id: str,
    model: str,
    request_json: str,
    response_text: str,
) -> None:
    if session is not None:
        await remember(db, session.id, turns)
        return
    await persist_completion(
        db,
        tenant_id=tenant_id,
        api_key_id=api_key_id,
        model=model,
        streamed=False,
        request_json=request_json,
        response_text=response_text,
        result=final,
    )
