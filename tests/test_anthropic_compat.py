import json

import pytest

from tests.fakes import FakeAgentRuntime
from tests.test_openai_compat import _RecordingRuntime, _events, _tool_events

_BASH_TOOL = {
    "name": "Bash",
    "description": "Run a shell command",
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


def _tool_events_named(name: str) -> list[dict]:
    evs = _tool_events()
    evs[1]["name"] = f"mcp__client__{name}"
    return evs


def _sse_events(text: str) -> list[tuple[str, dict]]:
    out = []
    for frame in text.strip().split("\n\n"):
        ev, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        out.append((ev, data))
    return out


@pytest.mark.anyio
async def test_messages_blocking_text(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/messages",
        json={"model": "sonnet", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message"
    assert body["role"] == "assistant"
    assert body["model"] == "claude-sonnet-5"
    assert body["content"] == [{"type": "text", "text": "Hello"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] == 3
    assert body["usage"]["output_tokens"] == 1


@pytest.mark.anyio
async def test_messages_x_api_key_header_accepted(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())
    token = authed_client.headers["Authorization"].split(" ", 1)[1]

    r = await authed_client.client.post(
        "/v1/messages",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers={"x-api-key": token},
    )

    assert r.status_code == 200


@pytest.mark.anyio
async def test_messages_tools_return_tool_use_block(authed_client):
    runtime = _RecordingRuntime(_tool_events_named("Bash"))
    authed_client.app.state.runtime = runtime

    r = await authed_client.client.post(
        "/v1/messages",
        json={
            "model": "sonnet",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [_BASH_TOOL],
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["stop_reason"] == "tool_use"
    (block,) = body["content"]
    assert block == {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    assert runtime.cfg.max_turns == 1
    assert runtime.cfg.tools == []
    assert runtime.cfg.allowed_tools == ["mcp__client__Bash"]


@pytest.mark.anyio
async def test_messages_streaming_anthropic_events(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_tool_events_named("Bash"))

    r = await authed_client.client.post(
        "/v1/messages",
        json={
            "model": "sonnet",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [_BASH_TOOL],
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    names = [e for e, _ in events]
    assert names[0] == "message_start"
    assert names[-1] == "message_stop"
    start = next(d for e, d in events if e == "content_block_start")
    assert start["content_block"]["type"] == "tool_use"
    assert start["content_block"]["name"] == "Bash"
    delta = next(d for e, d in events if e == "content_block_delta")
    assert json.loads(delta["delta"]["partial_json"]) == {"command": "ls"}
    md = next(d for e, d in events if e == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use"
    assert md["usage"]["input_tokens"] == 10


@pytest.mark.anyio
async def test_messages_history_flattened_and_logged(authed_client):
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.db.models import CompletionLog

    runtime = _RecordingRuntime(_events())
    authed_client.app.state.runtime = runtime

    r = await authed_client.client.post(
        "/v1/messages",
        json={
            "model": "sonnet",
            "max_tokens": 100,
            "system": [{"type": "text", "text": "You are Claude Code."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "list files"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Listing."},
                        {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": [{"type": "text", "text": "README.md"}],
                        }
                    ],
                },
            ],
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    p = runtime.cfg.prompt
    assert runtime.cfg.system_prompt is None
    assert "<system_instructions>\nYou are Claude Code.\n</system_instructions>" in p
    assert "User: list files" in p
    assert 'Assistant: Listing.\nCalled tools:\n[toolu_1] Bash({"command": "ls"})' in p
    assert "Tool result [toolu_1]:\nREADME.md" in p

    async with SessionLocal() as db:
        row = (await db.execute(select(CompletionLog))).scalars().one()
    logged = json.loads(row.request_json)
    assert logged[0] == {"role": "system", "content": "You are Claude Code."}
    assert logged[2]["tool_calls"][0]["function"]["name"] == "Bash"
    assert logged[3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "README.md"}


@pytest.mark.anyio
async def test_messages_error_result_returns_502(authed_client):
    events = [
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "boom",
            "session_id": "c1",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            "cost_usd": 0.0,
            "duration_ms": 5,
            "num_turns": 0,
        }
    ]
    authed_client.app.state.runtime = FakeAgentRuntime(events)

    r = await authed_client.client.post(
        "/v1/messages",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=authed_client.headers,
    )

    assert r.status_code == 502
    assert r.json()["error"]["message"] == "boom"


@pytest.mark.anyio
async def test_messages_streaming_error_event(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime([{"type": "error", "message": "boom"}])

    r = await authed_client.client.post(
        "/v1/messages",
        json={"model": "sonnet", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        headers=authed_client.headers,
    )

    events = _sse_events(r.text)
    err = next(d for e, d in events if e == "error")
    assert err["error"]["message"] == "boom"
    assert not any(e == "message_stop" for e, _ in events)


@pytest.mark.anyio
async def test_count_tokens(authed_client):
    r = await authed_client.client.post(
        "/v1/messages/count_tokens",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "x" * 400}]},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.json()["input_tokens"] == 100
