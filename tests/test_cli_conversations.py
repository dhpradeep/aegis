import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import CompletionLog, Event, Session
from tests.test_openai_compat import _RecordingRuntime, _events, _tool_events

_BASH_OAI = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
    },
}
_BASH_ANT = {
    "name": "Bash",
    "description": "Run a shell command",
    "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
}


async def _sessions() -> list[Session]:
    async with SessionLocal() as db:
        return (await db.execute(select(Session))).scalars().all()


async def _count(model) -> int:
    async with SessionLocal() as db:
        return len((await db.execute(select(model))).scalars().all())


@pytest.mark.anyio
async def test_openai_agentic_request_creates_session_and_resumes(authed_client):
    runtime = _RecordingRuntime(_tool_events())
    authed_client.app.state.runtime = runtime

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "You are opencode."},
                {"role": "user", "content": "list files"},
            ],
            "tools": [_BASH_OAI],
        },
        headers={**authed_client.headers, "user-agent": "opencode/1.0"},
    )
    assert r.status_code == 200
    assert r.json()["choices"][0]["finish_reason"] == "tool_calls"

    (session,) = await _sessions()
    assert session.profile == "opencode"
    assert session.title == "list files"
    assert session.sdk_session_id == "c1"
    assert session.status == "active"
    assert session.conv_turns == 1
    assert runtime.cfg.cwd == session.workspace_path
    assert runtime.cfg.resume is None
    assert "<system_instructions>" in runtime.cfg.prompt
    assert await _count(CompletionLog) == 0

    async with SessionLocal() as db:
        events = (
            await db.execute(select(Event).where(Event.session_id == session.id).order_by(Event.seq))
        ).scalars().all()
    assert [e.type for e in events][:3] == ["user_message", "system_prompt", "init"]
    assert json.loads(events[0].payload_json)["text"] == "list files"
    assert json.loads(events[1].payload_json)["text"] == "You are opencode."

    runtime = _RecordingRuntime(_events())
    authed_client.app.state.runtime = runtime
    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "You are opencode."},
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "toolu_1",
                            "type": "function",
                            "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "toolu_1", "content": "README.md"},
                {"role": "user", "content": "thanks"},
            ],
            "tools": [_BASH_OAI],
        },
        headers={**authed_client.headers, "user-agent": "opencode/1.0"},
    )
    assert r.status_code == 200

    (session,) = await _sessions()
    assert session.conv_turns == 4
    async with SessionLocal() as db:
        types = [
            e.type
            for e in (await db.execute(select(Event).where(Event.session_id == session.id).order_by(Event.seq))).scalars().all()
        ]
    assert types.count("system_prompt") == 1
    assert runtime.cfg.resume == "c1"
    assert "<system_instructions>" not in runtime.cfg.prompt
    assert "User: list files" not in runtime.cfg.prompt
    assert "Called tools" not in runtime.cfg.prompt
    assert runtime.cfg.prompt == "Tool result [toolu_1]:\nREADME.md\n\nUser: thanks"
    assert await _count(CompletionLog) == 0


@pytest.mark.anyio
async def test_non_agentic_request_stays_stateless(authed_client):
    authed_client.app.state.runtime = _RecordingRuntime(_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}]},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert await _sessions() == []
    assert await _count(CompletionLog) == 1


@pytest.mark.anyio
async def test_diverged_history_starts_new_session(authed_client):
    authed_client.app.state.runtime = _RecordingRuntime(_events())
    base = {"model": "sonnet", "tools": [_BASH_OAI]}

    await authed_client.client.post(
        "/v1/chat/completions",
        json={**base, "messages": [{"role": "user", "content": "first"}]},
        headers=authed_client.headers,
    )
    await authed_client.client.post(
        "/v1/chat/completions",
        json={**base, "messages": [{"role": "user", "content": "unrelated"}, {"role": "user", "content": "x"}]},
        headers=authed_client.headers,
    )

    assert len(await _sessions()) == 2


@pytest.mark.anyio
async def test_anthropic_agentic_request_routes_to_session(authed_client):
    evs = _tool_events()
    evs[1]["name"] = "mcp__client__Bash"
    runtime = _RecordingRuntime(evs)
    authed_client.app.state.runtime = runtime
    headers = {**authed_client.headers, "user-agent": "claude-cli/2.1.0 (external, cli)"}

    r = await authed_client.client.post(
        "/v1/messages",
        json={
            "model": "sonnet",
            "max_tokens": 100,
            "system": "You are Claude Code.",
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [_BASH_ANT],
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["stop_reason"] == "tool_use"
    (session,) = await _sessions()
    assert session.profile == "claude-code"
    assert session.sdk_session_id == "c1"

    runtime = _RecordingRuntime(_events())
    authed_client.app.state.runtime = runtime
    r = await authed_client.client.post(
        "/v1/messages",
        json={
            "model": "sonnet",
            "max_tokens": 100,
            "system": "You are Claude Code.",
            "messages": [
                {"role": "user", "content": "list files"},
                {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}],
                },
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "README.md"}],
                },
            ],
            "tools": [_BASH_ANT],
        },
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["content"] == [{"type": "text", "text": "Hello"}]
    assert len(await _sessions()) == 1
    assert runtime.cfg.resume == "c1"
    assert runtime.cfg.prompt == "Tool result [toolu_1]:\nREADME.md"
