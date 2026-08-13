"""OpenAI-compatible shim: `POST /v1/chat/completions`, `GET /v1/models`.

Each chat-completion call maps onto a single stateless agent run: messages
are flattened into one prompt in a throwaway `_chat/<uuid>` workspace and the
terminal `result` event becomes the OpenAI response (or SSE chunk stream).

Client-side tool calling: request `tools` are exposed to the model as an
in-process SDK MCP server whose handlers never execute anything — the run is
capped at one assistant turn, each `tool_use` is captured and returned as
OpenAI `tool_calls`, and the client executes them locally and sends results
back as `role: "tool"` messages. Without `tools`, the shim uses its fixed
server-side read-only chat profile.

The streaming path opens its own `SessionLocal` (the request-scoped `db`
dependency is torn down before the generator body runs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server
from claude_agent_sdk import tool as sdk_tool
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import SessionLocal
from app.db.models import ApiKey, CompletionLog, Tenant, Usage
from app.schemas.openai import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    ModelCard,
    ModelList,
    ResponseMessage,
    ToolCall,
    ToolCallFunction,
    ToolDef,
)
from app.services import ratelimit
from app.services.agent.runtime import RunConfig, run_and_collect
from app.services.models import EFFORT_LEVELS, get_models
from app.services.ratelimit import check_daily_cost, check_rpm

router = APIRouter(prefix="/v1", tags=["openai-compat"])

# Detached persist tasks (kept referenced until done so they aren't GC'd).
_persist_tasks: set[asyncio.Task] = set()

MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

CHAT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch"]
CHAT_PERMISSION_MODE = "default"
CHAT_MAX_TURNS = 30

CLIENT_TOOLS_SERVER = "client"
_CLIENT_TOOL_PREFIX = f"mcp__{CLIENT_TOOLS_SERVER}__"

_EFFORT_SYNONYMS = {"minimal": "low"}


def _resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


async def _effective_model(db: AsyncSession, body: ChatCompletionRequest, tenant_id: str) -> str:
    chosen = body.model
    if not chosen or chosen == "default":
        tenant = await db.get(Tenant, tenant_id)
        chosen = (tenant.default_model if tenant else None) or get_settings().default_model
    return _resolve_model(chosen)


def _effective_effort(value: str | None) -> str | None:
    if value is None:
        return None
    value = _EFFORT_SYNONYMS.get(value, value)
    return value if value in EFFORT_LEVELS else None


def _content_to_text(content: str | list[dict[str, Any]] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text":
            parts.append(part.get("text", ""))
    return "".join(parts)


def _flatten_messages(messages: list[ChatMessage]) -> tuple[str | None, str]:
    system_parts: list[str] = []
    turns: list[str] = []
    for msg in messages:
        text = _content_to_text(msg.content)
        if msg.role == "system":
            system_parts.append(text)
        elif msg.role == "tool":
            call_id = msg.tool_call_id or "unknown"
            turns.append(f"Tool result [{call_id}]:\n{text}")
        elif msg.role == "assistant" and msg.tool_calls:
            calls = "\n".join(
                f"[{c.id}] {c.function.name}({c.function.arguments})" for c in msg.tool_calls
            )
            prefix = f"Assistant: {text}\n" if text else "Assistant: "
            turns.append(f"{prefix}Called tools:\n{calls}")
        else:
            turns.append(f"{msg.role.capitalize()}: {text}")
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(turns)
    return system_prompt, prompt


def _client_tools_requested(body: ChatCompletionRequest) -> list[ToolDef]:
    if not body.tools or body.tool_choice == "none":
        return []
    return body.tools


def _build_client_tools(tools: list[ToolDef]) -> tuple[dict, list[str]]:
    sdk_tools = []
    allowed: list[str] = []
    for t in tools:
        fn = t.function
        schema = fn.parameters
        # create_sdk_mcp_server treats the dict as raw JSON Schema only when
        # "type" and "properties" are both present.
        if not isinstance(schema, dict) or "properties" not in schema:
            schema = {"type": "object", "properties": {}}

        async def _handler(args: dict, _name: str = fn.name) -> dict:
            return {
                "content": [
                    {"type": "text", "text": "Tool call captured; executed by the client."}
                ]
            }

        sdk_tools.append(sdk_tool(fn.name, fn.description or fn.name, schema)(_handler))
        allowed.append(f"{_CLIENT_TOOL_PREFIX}{fn.name}")
    server = create_sdk_mcp_server(CLIENT_TOOLS_SERVER, tools=sdk_tools)
    return {CLIENT_TOOLS_SERVER: server}, allowed


def _tool_call_from_event(ev: dict) -> ToolCall | None:
    name = ev.get("name") or ""
    if not name.startswith(_CLIENT_TOOL_PREFIX):
        return None
    return ToolCall(
        id=ev.get("id") or ("call_" + uuid.uuid4().hex),
        function=ToolCallFunction(
            name=name[len(_CLIENT_TOOL_PREFIX):],
            arguments=json.dumps(ev.get("input") or {}),
        ),
    )


def _scratch_workspace() -> Path:
    ws = Path(get_settings().workspace_root) / "_chat" / uuid.uuid4().hex
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _build_config(body: ChatCompletionRequest, cwd: str, model: str) -> RunConfig:
    system_prompt, prompt = _flatten_messages(body.messages)
    # Client system prompts ride inside the prompt body: subscription-auth
    # requests get rejected (bogus "out of extra usage" 400) when a large
    # system prompt replaces or extends the CLI's own.
    if system_prompt:
        prompt = f"<system_instructions>\n{system_prompt}\n</system_instructions>\n\n{prompt}"
    client_tools = _client_tools_requested(body)
    if client_tools:
        # One assistant turn: the model either answers or requests tool calls;
        # either way control returns to the client. Server-side tools stay off
        # so nothing shadows the client's own.
        mcp_servers, allowed_tools = _build_client_tools(client_tools)
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
        resume=None,
        timeout_s=get_settings().run_timeout_s,
        effort=_effective_effort(body.reasoning_effort),
        tools=builtin_tools,
    )


async def _persist_completion(
    db: AsyncSession,
    *,
    tenant_id: str,
    api_key_id: str,
    model: str,
    streamed: bool,
    request_messages: list[ChatMessage],
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
            request_json=json.dumps([m.model_dump() for m in request_messages]),
            response_text=response_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
        )
    )
    await db.commit()


async def _persist_streamed(
    *,
    tenant_id: str,
    api_key_id: str,
    model: str,
    request_messages: list[ChatMessage],
    response_text: str,
    result: dict,
) -> None:
    try:
        async with SessionLocal() as db:
            await _persist_completion(
                db,
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                model=model,
                streamed=True,
                request_messages=request_messages,
                response_text=response_text,
                result=result,
            )
    except Exception:
        logging.getLogger("app").exception("failed to persist streamed completion")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _chunk(
    chat_id: str,
    created: int,
    model: str,
    delta: dict,
    finish_reason: str | None,
) -> str:
    return _sse(
        {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def _usage_chunk(chat_id: str, created: int, model: str, result: dict) -> str:
    usage = result.get("usage") or {}
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    return _sse(
        {
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }
    )


def _error_sse(message: str) -> str:
    return _sse({"error": {"message": message, "type": "server_error", "code": "agent_error"}})


async def _stream_chat(
    runtime,
    cfg: RunConfig,
    *,
    chat_id: str,
    created: int,
    model: str,
    include_usage: bool,
    tenant_id: str,
    api_key_id: str,
    request_messages: list[ChatMessage],
) -> AsyncIterator[str]:
    final: dict | None = None
    parts: list[str] = []
    tool_calls: list[ToolCall] = []
    error_message: str | None = None
    try:
        yield _chunk(chat_id, created, model, {"role": "assistant"}, None)
        async for ev in runtime.stream(cfg):
            if ev["type"] == "text":
                parts.append(ev["text"])
                yield _chunk(chat_id, created, model, {"content": ev["text"]}, None)
            elif ev["type"] == "tool_use":
                call = _tool_call_from_event(ev)
                if call is not None:
                    delta = {
                        "tool_calls": [
                            {
                                "index": len(tool_calls),
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.function.name,
                                    "arguments": call.function.arguments,
                                },
                            }
                        ]
                    }
                    tool_calls.append(call)
                    yield _chunk(chat_id, created, model, delta, None)
            elif ev["type"] == "result":
                final = ev
            elif ev["type"] == "error" and final is None:
                final = ev
                error_message = ev.get("message") or "Agent run failed"

        # Captured tool calls trump an error-flagged result: max_turns=1 makes
        # the SDK report hitting the turn limit as an error by design.
        if error_message is None and final is not None and final.get("type") == "result":
            if final.get("is_error") and not tool_calls:
                error_message = str(final.get("result") or "".join(parts) or "Agent run failed")

        if error_message is not None:
            yield _error_sse(error_message)
        else:
            finish = "tool_calls" if tool_calls else "stop"
            yield _chunk(chat_id, created, model, {}, finish)
            if include_usage and final is not None and final.get("type") == "result":
                yield _usage_chunk(chat_id, created, model, final)
        yield "data: [DONE]\n\n"
    finally:
        ratelimit.run_gate.release()
        # Persist in a detached task: if the client disconnected, this
        # generator is being cancelled and awaiting DB work here would be
        # interrupted mid-write, poisoning the pooled connection.
        if final is not None and final.get("type") == "result":
            task = asyncio.get_running_loop().create_task(
                _persist_streamed(
                    tenant_id=tenant_id,
                    api_key_id=api_key_id,
                    model=model,
                    request_messages=request_messages,
                    response_text="".join(parts),
                    result=final,
                )
            )
            _persist_tasks.add(task)
            task.add_done_callback(_persist_tasks.discard)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass  # client gone; the shielded task finishes on its own


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
):
    await check_rpm(db, key)
    await check_daily_cost(db, key)

    model = await _effective_model(db, body, key.tenant_id)

    # Create the scratch workspace (mkdir) before acquiring the run gate, so
    # a mkdir OSError here doesn't leak a gate slot.
    ws = _scratch_workspace()
    cfg = _build_config(body, str(ws), model)

    await ratelimit.run_gate.acquire()
    runtime = request.app.state.runtime
    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())

    if body.stream:
        # The stream generator owns releasing the run gate (in its own
        # `finally`) since it executes after this route function returns.
        return StreamingResponse(
            _stream_chat(
                runtime,
                cfg,
                chat_id=chat_id,
                created=created,
                model=model,
                include_usage=bool(body.stream_options and body.stream_options.include_usage),
                tenant_id=key.tenant_id,
                api_key_id=key.id,
                request_messages=body.messages,
            ),
            media_type="text/event-stream",
        )

    try:
        texts: list[str] = []
        tool_calls: list[ToolCall] = []

        async def _collect(ev: dict) -> None:
            if ev["type"] == "text":
                texts.append(ev["text"])
            elif ev["type"] == "tool_use":
                call = _tool_call_from_event(ev)
                if call is not None:
                    tool_calls.append(call)

        final = await run_and_collect(runtime, cfg, _collect)
        if final is None or final.get("type") != "result":
            message = (final or {}).get("message", "Agent run failed")
            raise ApiError.agent_error(message)
        if final.get("is_error") and not tool_calls:
            raise ApiError.agent_error(
                str(final.get("result") or "".join(texts) or "Agent run failed")
            )

        response_text = "".join(texts)
        await _persist_completion(
            db,
            tenant_id=key.tenant_id,
            api_key_id=key.id,
            model=model,
            streamed=False,
            request_messages=body.messages,
            response_text=response_text,
            result=final,
        )

        usage = final.get("usage") or {}
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
        return ChatCompletionResponse(
            id=chat_id,
            created=created,
            model=model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ResponseMessage(
                        role="assistant",
                        content=response_text if response_text else None,
                        tool_calls=tool_calls or None,
                    ),
                    finish_reason="tool_calls" if tool_calls else "stop",
                )
            ],
            usage=ChatCompletionUsage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )
    finally:
        ratelimit.run_gate.release()


@router.get("/models", response_model=ModelList)
async def list_models(key: ApiKey = Depends(require_key)) -> ModelList:
    models = await get_models()
    cards = [ModelCard(id=m["id"]) for m in models]
    cards.append(ModelCard(id="default"))
    return ModelList(data=cards)
