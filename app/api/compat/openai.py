"""OpenAI-compatible shim: `POST /v1/chat/completions`, `GET /v1/models`.

Requests without client tools run as one stateless agent run in a throwaway
workspace and are logged as completions. Requests carrying `tools` (agent
CLIs) are routed into a Session: the tools are bridged through an in-process
MCP server, the run is capped at one assistant turn, captured `tool_use`
blocks come back as `tool_calls`, and the next request — recognized by its
transcript prefix — resumes the same SDK session with only the new turns.
"""

from __future__ import annotations

import json
import time
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
)
from app.services import ratelimit
from app.services.models import get_models
from app.services.ratelimit import check_daily_cost, check_rpm

router = APIRouter(prefix="/v1", tags=["openai-compat"])


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


def _flatten_messages(messages: list[ChatMessage]) -> tuple[str | None, list[Turn]]:
    system_parts: list[str] = []
    turns: list[Turn] = []
    for msg in messages:
        text = _content_to_text(msg.content)
        if msg.role == "system":
            system_parts.append(text)
        elif msg.role == "tool":
            call_id = msg.tool_call_id or "unknown"
            turns.append(("tool", f"Tool result [{call_id}]:\n{text}"))
        elif msg.role == "assistant" and msg.tool_calls:
            calls = "\n".join(
                f"[{c.id}] {c.function.name}({c.function.arguments})" for c in msg.tool_calls
            )
            prefix = f"Assistant: {text}\n" if text else "Assistant: "
            turns.append(("assistant", f"{prefix}Called tools:\n{calls}"))
        else:
            turns.append((msg.role, f"{msg.role.capitalize()}: {text}"))
    system_prompt = "\n\n".join(system_parts) if system_parts else None
    return system_prompt, turns


def _client_tools(body: ChatCompletionRequest) -> list[ClientTool]:
    if not body.tools or body.tool_choice == "none":
        return []
    return [
        ClientTool(t.function.name, t.function.description, t.function.parameters)
        for t in body.tools
    ]


def _tool_call_from_event(ev: dict) -> ToolCall | None:
    name = client_tool_name(ev)
    if name is None:
        return None
    return ToolCall(
        id=ev.get("id") or ("call_" + uuid.uuid4().hex),
        function=ToolCallFunction(name=name, arguments=json.dumps(ev.get("input") or {})),
    )


def _request_json(messages: list[ChatMessage]) -> str:
    return json.dumps([m.model_dump() for m in messages])


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _chunk(chat_id: str, created: int, model: str, delta: dict, finish_reason: str | None) -> str:
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
    events: AsyncIterator[dict],
    *,
    chat_id: str,
    created: int,
    model: str,
    include_usage: bool,
    session: Session | None,
    turns: list[Turn],
    tenant_id: str,
    api_key_id: str,
    request_json: str,
) -> AsyncIterator[str]:
    final: dict | None = None
    parts: list[str] = []
    tool_calls: list[ToolCall] = []
    error_message: str | None = None
    try:
        yield _chunk(chat_id, created, model, {"role": "assistant"}, None)
        async for ev in events:
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


@router.post("/chat/completions", response_model=None)
async def chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
):
    await check_rpm(db, key)
    await check_daily_cost(db, key)

    model = await effective_model(db, body.model, key.tenant_id)
    system_prompt, turns = _flatten_messages(body.messages)
    client_tools = _client_tools(body)
    session, delta = await resolve_conversation(
        db,
        key.tenant_id,
        turns,
        agentic=bool(client_tools),
        profile=client_profile(request, "openai-client"),
    )
    try:
        resuming = session is not None and session.sdk_session_id is not None
        cfg = build_run_config(
            prompt=prompt_from_turns(delta),
            system_prompt=None if resuming else system_prompt,
            client_tools=client_tools,
            model=model,
            effort=effective_effort(body.reasoning_effort),
            cwd=session.workspace_path if session else str(scratch_workspace()),
            resume=session.sdk_session_id if session else None,
        )
        await ratelimit.run_gate.acquire()
    except Exception:
        await release_session(db, session)
        raise

    chat_id = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    request_json = _request_json(body.messages)
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
            _stream_chat(
                events,
                chat_id=chat_id,
                created=created,
                model=model,
                include_usage=bool(body.stream_options and body.stream_options.include_usage),
                session=session,
                turns=turns,
                tenant_id=key.tenant_id,
                api_key_id=key.id,
                request_json=request_json,
            ),
            media_type="text/event-stream",
        )

    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    final: dict | None = None
    async for ev in events:
        if ev["type"] == "text":
            texts.append(ev["text"])
        elif ev["type"] == "tool_use":
            call = _tool_call_from_event(ev)
            if call is not None:
                tool_calls.append(call)
        elif ev["type"] == "result":
            final = ev
        elif ev["type"] == "error" and final is None:
            final = ev

    if final is None or final.get("type") != "result":
        raise ApiError.agent_error((final or {}).get("message", "Agent run failed"))
    if final.get("is_error") and not tool_calls:
        raise ApiError.agent_error(str(final.get("result") or "".join(texts) or "Agent run failed"))

    response_text = "".join(texts)
    await after_blocking(
        db,
        session=session,
        turns=turns,
        final=final,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        model=model,
        request_json=request_json,
        response_text=response_text,
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


@router.get("/models", response_model=ModelList)
async def list_models(key: ApiKey = Depends(require_key)) -> ModelList:
    models = await get_models()
    cards = [ModelCard(id=m["id"], display_name=m.get("label")) for m in models]
    cards.append(ModelCard(id="default", display_name="Tenant default"))
    return ModelList(data=cards)
