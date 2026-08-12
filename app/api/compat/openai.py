"""OpenAI-compatible shim: `POST /v1/chat/completions`, `GET /v1/models`.

Lets existing OpenAI/LangChain clients point `base_url` at this platform.
Each chat-completion call is mapped onto a single **stateless** agent run
(no `resume`): the message list is flattened into one prompt in a throwaway
`_chat/<uuid>` workspace, run through the same `AgentRuntime` / rate-limit /
run-gate machinery as the native session routes, and the terminal `result`
event is mapped back into an OpenAI-shaped response (or SSE chunk stream).

Reuses `run_and_collect` (app.services.agent.runtime) for the blocking path.
The streaming path opens its own `SessionLocal` to persist the `Usage` row
(rather than the request-scoped `db` dependency) because — same as
`session_runner.run_session_message` — the streaming generator body runs
*after* the route function returns and FastAPI has already torn down the
request-scoped dependency's session.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

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
)
from app.services import ratelimit
from app.services.agent.runtime import RunConfig, run_and_collect
from app.services.ratelimit import check_daily_cost, check_rpm

router = APIRouter(prefix="/v1", tags=["openai-compat"])

# Model alias -> real model id. Unknown values pass through unchanged so
# clients can also address models directly.
MODEL_ALIASES: dict[str, str] = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}

# Fixed "chat" profile: read-only tools, default permission mode. The shim
# doesn't expose session/profile selection — it's a convenience layer, not a
# full platform surface.
CHAT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "WebSearch"]
CHAT_PERMISSION_MODE = "default"
CHAT_MAX_TURNS = 30


def _resolve_model(model: str) -> str:
    return MODEL_ALIASES.get(model, model)


async def _effective_model(db: AsyncSession, body: ChatCompletionRequest, tenant_id: str) -> str:
    """Resolve the model to run: request `model`, else the tenant default,
    else the global `Settings.default_model`. Aliases are then expanded."""
    chosen = body.model
    if not chosen:
        tenant = await db.get(Tenant, tenant_id)
        chosen = (tenant.default_model if tenant else None) or get_settings().default_model
    return _resolve_model(chosen)


def _flatten_messages(messages: list[ChatMessage]) -> tuple[str | None, str]:
    """Split `messages` into a system prompt and a flattened transcript.

    System messages are joined into the system prompt; every other role is
    rendered as `<Role>: <content>` lines in the transcript — the shim is
    stateless (no `resume`), so prior turns are carried as plain text rather
    than via the SDK's native conversation state.
    """
    system_parts: list[str] = []
    turns: list[str] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            turns.append(f"{msg.role.capitalize()}: {msg.content}")
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    prompt = "\n\n".join(turns)
    return system_prompt, prompt


def _scratch_workspace() -> Path:
    """Create a throwaway per-request workspace under `_chat/<uuid>`.

    Left in place for the `_chat` TTL sweep (see `workspaces.cleanup_expired`)
    rather than removed synchronously here.
    """
    ws = Path(get_settings().workspace_root) / "_chat" / uuid.uuid4().hex
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _build_config(body: ChatCompletionRequest, cwd: str, model: str) -> RunConfig:
    system_prompt, prompt = _flatten_messages(body.messages)
    return RunConfig(
        prompt=prompt,
        cwd=cwd,
        system_prompt=system_prompt,
        allowed_tools=CHAT_ALLOWED_TOOLS,
        permission_mode=CHAT_PERMISSION_MODE,
        mcp_servers={},
        model=model,
        max_turns=CHAT_MAX_TURNS,
        resume=None,
        timeout_s=get_settings().run_timeout_s,
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
    """Write both the Usage row (for billing) and a CompletionLog row (the
    durable record of this individual chat completion)."""
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


def _chunk(chat_id: str, model: str, delta: dict, finish_reason: str | None) -> str:
    payload = {
        "id": chat_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload)}\n\n"


async def _stream_chat(
    runtime,
    cfg: RunConfig,
    *,
    chat_id: str,
    model: str,
    tenant_id: str,
    api_key_id: str,
    request_messages: list[ChatMessage],
) -> AsyncIterator[str]:
    """Drive `runtime.stream(cfg)` as OpenAI-shaped SSE chunks.

    Releases the run gate and persists the usage + completion-log rows in a
    `finally` so both happen even if the client disconnects mid-stream.
    """
    final: dict | None = None
    parts: list[str] = []
    try:
        async for ev in runtime.stream(cfg):
            if ev["type"] == "text":
                parts.append(ev["text"])
                yield _chunk(chat_id, model, {"content": ev["text"]}, None)
            elif ev["type"] == "result":
                final = ev
            elif ev["type"] == "error" and final is None:
                final = ev
        yield _chunk(chat_id, model, {}, "stop")
        yield "data: [DONE]\n\n"
    finally:
        ratelimit.run_gate.release()
        if final is not None and final.get("type") == "result":
            async with SessionLocal() as db:
                await _persist_completion(
                    db,
                    tenant_id=tenant_id,
                    api_key_id=api_key_id,
                    model=model,
                    streamed=True,
                    request_messages=request_messages,
                    response_text="".join(parts),
                    result=final,
                )


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

    if body.stream:
        # The stream generator owns releasing the run gate (in its own
        # `finally`) since it executes after this route function returns.
        return StreamingResponse(
            _stream_chat(
                runtime,
                cfg,
                chat_id=chat_id,
                model=model,
                tenant_id=key.tenant_id,
                api_key_id=key.id,
                request_messages=body.messages,
            ),
            media_type="text/event-stream",
        )

    try:
        texts: list[str] = []

        async def _collect(ev: dict) -> None:
            if ev["type"] == "text":
                texts.append(ev["text"])

        final = await run_and_collect(runtime, cfg, _collect)
        if final is None or final.get("type") != "result":
            message = (final or {}).get("message", "Agent run failed")
            raise ApiError.agent_error(message)

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
            model=model,
            choices=[
                ChatCompletionChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="".join(texts)),
                    finish_reason="stop",
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
    return ModelList(data=[ModelCard(id=alias) for alias in MODEL_ALIASES])
