"""Anthropic Messages API shim: `POST /v1/messages`,
`POST /v1/messages/count_tokens`.

Lets Claude Code (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`) and other
Anthropic-SDK clients run against this platform. Same model as the OpenAI
shim: one stateless agent run per request, client `tools` bridged through an
in-process MCP server, captured `tool_use` blocks handed back to the client
with `stop_reason: "tool_use"`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.compat.common import (
    ClientTool,
    after_blocking,
    after_stream,
    build_run_config,
    client_profile,
    client_tool_name,
    effective_effort,
    effective_model,
    release_session,
    resolve_conversation,
    run_events,
    scratch_workspace,
)
from app.api.compat.conversation import Turn, display_text, prompt_from_turns
from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey, Session
from app.schemas.anthropic import AnthropicMessage, CountTokensRequest, MessagesRequest
from app.services import ratelimit
from app.services.agent.runtime import RunConfig
from app.services.ratelimit import check_daily_cost, check_rpm

router = APIRouter(prefix="/v1", tags=["anthropic-compat"])


def _blocks_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(
        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
    )


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = _blocks_text(content)
        return text if text else json.dumps(content)
    return json.dumps(content)


def _flatten(
    system: str | list[dict[str, Any]] | None, messages: list[AnthropicMessage]
) -> tuple[str | None, list[Turn]]:
    turns: list[Turn] = []
    for msg in messages:
        role = msg.role.capitalize()
        if isinstance(msg.content, str):
            turns.append((msg.role, f"{role}: {msg.content}"))
            continue
        texts: list[str] = []
        calls: list[str] = []
        for b in msg.content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                calls.append(f"[{b.get('id', '')}] {b.get('name', '')}({json.dumps(b.get('input') or {})})")
            elif t == "tool_result":
                turns.append(
                    (
                        "tool",
                        f"Tool result [{b.get('tool_use_id', 'unknown')}]:\n{_tool_result_text(b.get('content'))}",
                    )
                )
            elif t in ("image", "document"):
                texts.append(f"[{t} omitted]")
        text = "".join(texts)
        if calls:
            prefix = f"{role}: {text}\n" if text else f"{role}: "
            turns.append((msg.role, prefix + "Called tools:\n" + "\n".join(calls)))
        elif text:
            turns.append((msg.role, f"{role}: {text}"))
    system_text = _blocks_text(system) or None
    return system_text, turns


def _log_messages(system: str | list[dict[str, Any]] | None, messages: list[AnthropicMessage]) -> str:
    out: list[dict] = []
    system_text = _blocks_text(system)
    if system_text:
        out.append({"role": "system", "content": system_text})
    for msg in messages:
        if isinstance(msg.content, str):
            out.append({"role": msg.role, "content": msg.content})
            continue
        texts: list[str] = []
        calls: list[dict] = []
        for b in msg.content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                texts.append(b.get("text", ""))
            elif t == "tool_use":
                calls.append(
                    {
                        "id": b.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": b.get("name", ""),
                            "arguments": json.dumps(b.get("input") or {}),
                        },
                    }
                )
            elif t == "tool_result":
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": b.get("tool_use_id"),
                        "content": _tool_result_text(b.get("content")),
                    }
                )
        if texts or calls:
            entry: dict = {"role": msg.role, "content": "".join(texts) or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
    return json.dumps(out)


def _client_tools(body: MessagesRequest) -> list[ClientTool]:
    if not body.tools or (body.tool_choice or {}).get("type") == "none":
        return []
    return [
        ClientTool(t.name, t.description, t.input_schema)
        for t in body.tools
        if t.input_schema is not None
    ]



def _tool_use_block(ev: dict) -> dict | None:
    name = client_tool_name(ev)
    if name is None:
        return None
    return {
        "type": "tool_use",
        "id": ev.get("id") or ("toolu_" + uuid.uuid4().hex),
        "name": name,
        "input": ev.get("input") or {},
    }


def _usage(result: dict | None) -> dict:
    usage = (result or {}).get("usage") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": usage.get("cache_read_tokens", 0),
    }


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def _error_sse(message: str) -> str:
    return _sse("error", {"type": "error", "error": {"type": "api_error", "message": message}})


async def _stream(
    events: AsyncIterator[dict],
    *,
    msg_id: str,
    model: str,
    session: Session | None,
    turns: list[Turn],
    tenant_id: str,
    api_key_id: str,
    request_json: str,
) -> AsyncIterator[str]:
    final: dict | None = None
    parts: list[str] = []
    tool_uses = 0
    index = 0
    error_message: str | None = None
    try:
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        async for ev in events:
            if ev["type"] == "text":
                parts.append(ev["text"])
                yield _sse(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                )
                yield _sse(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": ev["text"]}},
                )
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                index += 1
            elif ev["type"] == "tool_use":
                block = _tool_use_block(ev)
                if block is None:
                    continue
                tool_uses += 1
                yield _sse(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": {**block, "input": {}}},
                )
                yield _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                    },
                )
                yield _sse("content_block_stop", {"type": "content_block_stop", "index": index})
                index += 1
            elif ev["type"] == "result":
                final = ev
            elif ev["type"] == "error" and final is None:
                final = ev
                error_message = ev.get("message") or "Agent run failed"

        # Captured tool calls trump an error-flagged result (max_turns=1).
        if error_message is None and final is not None and final.get("type") == "result":
            if final.get("is_error") and not tool_uses:
                error_message = str(final.get("result") or "".join(parts) or "Agent run failed")

        if error_message is not None:
            yield _error_sse(error_message)
        else:
            yield _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use" if tool_uses else "end_turn", "stop_sequence": None},
                    "usage": _usage(final),
                },
            )
            yield _sse("message_stop", {"type": "message_stop"})
    finally:
        await after_stream(
            session=session,
            turns=turns,
            final=final,
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            model=model,
            request_json=request_json,
            response_text="".join(parts),
        )


@router.post("/messages", response_model=None)
async def messages(
    body: MessagesRequest,
    request: Request,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
):
    await check_rpm(db, key)
    await check_daily_cost(db, key)

    model = await effective_model(db, body.model, key.tenant_id)
    system_prompt, turns = _flatten(body.system, body.messages)
    client_tools = _client_tools(body)
    session, delta = await resolve_conversation(
        db,
        key.tenant_id,
        turns,
        agentic=bool(client_tools),
        profile=client_profile(request, "anthropic-client"),
    )
    try:
        resuming = session is not None and session.sdk_session_id is not None
        cfg = build_run_config(
            prompt=prompt_from_turns(delta),
            system_prompt=None if resuming else system_prompt,
            client_tools=client_tools,
            model=model,
            effort=effective_effort((body.output_config or {}).get("effort")),
            cwd=session.workspace_path if session else str(scratch_workspace()),
            resume=session.sdk_session_id if session else None,
        )
        await ratelimit.run_gate.acquire()
    except Exception:
        await release_session(db, session)
        raise

    request_json = _log_messages(body.system, body.messages)
    msg_id = "msg_" + uuid.uuid4().hex
    events = run_events(
        request.app.state.runtime,
        cfg,
        session,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        display_prompt=display_text(delta),
        system_prompt=None if resuming else system_prompt,
    )

    if body.stream:
        return StreamingResponse(
            _stream(
                events,
                msg_id=msg_id,
                model=model,
                session=session,
                turns=turns,
                tenant_id=key.tenant_id,
                api_key_id=key.id,
                request_json=request_json,
            ),
            media_type="text/event-stream",
        )

    content: list[dict] = []
    texts: list[str] = []
    tool_uses = 0
    final: dict | None = None
    async for ev in events:
        if ev["type"] == "text":
            texts.append(ev["text"])
            content.append({"type": "text", "text": ev["text"]})
        elif ev["type"] == "tool_use":
            block = _tool_use_block(ev)
            if block is not None:
                tool_uses += 1
                content.append(block)
        elif ev["type"] == "result":
            final = ev
        elif ev["type"] == "error" and final is None:
            final = ev

    if final is None or final.get("type") != "result":
        raise ApiError.agent_error((final or {}).get("message", "Agent run failed"))
    if final.get("is_error") and not tool_uses:
        raise ApiError.agent_error(str(final.get("result") or "".join(texts) or "Agent run failed"))

    await after_blocking(
        db,
        session=session,
        turns=turns,
        final=final,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        model=model,
        request_json=request_json,
        response_text="".join(texts),
    )
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if tool_uses else "end_turn",
        "stop_sequence": None,
        "usage": _usage(final),
    }


@router.post("/messages/count_tokens")
async def count_tokens(body: CountTokensRequest, key: ApiKey = Depends(require_key)) -> dict:
    # Rough estimate (~4 chars/token); the CLI only uses this for context display.
    size = len(_blocks_text(body.system)) + sum(
        len(m.content) if isinstance(m.content, str) else len(json.dumps(m.content))
        for m in body.messages
    )
    if body.tools:
        size += sum(len(json.dumps(t.model_dump(exclude_none=True))) for t in body.tools)
    return {"input_tokens": max(1, size // 4)}
