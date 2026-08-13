import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Usage
from tests.fakes import FakeAgentRuntime


def _events() -> list[dict]:
    return [
        {"type": "init", "session_id": "c1"},
        {"type": "text", "text": "Hello"},
        {
            "type": "result",
            "subtype": "success",
            "result": "Hello",
            "session_id": "c1",
            "usage": {"input_tokens": 3, "output_tokens": 1, "cache_read_tokens": 0},
            "cost_usd": 0.0,
            "duration_ms": 10,
            "num_turns": 1,
        },
    ]


@pytest.mark.anyio
async def test_chat_completions_blocking(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "claude-sonnet-5"
    assert body["choices"][0]["message"]["content"] == "Hello"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 3
    assert body["usage"]["completion_tokens"] == 1
    assert body["usage"]["total_tokens"] == 4

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(Usage).where(Usage.api_key_id == authed_client.key_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].session_id is None
        assert rows[0].tenant_id == authed_client.tenant_id


@pytest.mark.anyio
async def test_chat_completions_are_logged(authed_client):
    from app.db.models import CompletionLog

    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi there"}], "stream": False},
        headers=authed_client.headers,
    )

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(CompletionLog).where(CompletionLog.api_key_id == authed_client.key_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.model == "claude-sonnet-5"
        assert row.streamed is False
        assert row.response_text == "Hello"
        assert "hi there" in row.request_json
        assert row.tenant_id == authed_client.tenant_id


@pytest.mark.anyio
async def test_chat_completions_streaming(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "delta" in r.text
    assert r.text.strip().endswith("data: [DONE]")

    async with SessionLocal() as db:
        rows = (
            (await db.execute(select(Usage).where(Usage.api_key_id == authed_client.key_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].session_id is None


@pytest.mark.anyio
async def test_chat_completions_omitted_model_uses_global_default(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    # No model in the request and no tenant default -> global Settings default.
    assert r.json()["model"] == "claude-sonnet-5"


@pytest.mark.anyio
async def test_chat_completions_omitted_model_uses_tenant_default(authed_client):
    from app.db.models import Tenant

    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    # Set a per-tenant default of "haiku".
    async with SessionLocal() as db:
        tenant = await db.get(Tenant, authed_client.tenant_id)
        tenant.default_model = "haiku"
        await db.commit()

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.json()["model"] == "claude-haiku-4-5"


@pytest.mark.anyio
async def test_list_models(authed_client):
    r = await authed_client.client.get("/v1/models", headers=authed_client.headers)

    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = {m["id"] for m in body["data"]}
    assert {"claude-sonnet-5", "opus", "sonnet", "haiku", "default"} <= ids


@pytest.mark.anyio
async def test_chat_completions_missing_key_returns_401(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 401


# --- client-side tool calling -------------------------------------------------


class _RecordingRuntime(FakeAgentRuntime):
    def __init__(self, events: list[dict]) -> None:
        super().__init__(events)
        self.cfg = None

    async def stream(self, cfg=None):
        self.cfg = cfg
        async for ev in super().stream(cfg):
            yield ev


_BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "Run a shell command",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
}


def _tool_events() -> list[dict]:
    # Model calls a client tool, then hits max_turns=1 (error-flagged result).
    return [
        {"type": "init", "session_id": "c1"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "mcp__client__bash",
            "input": {"command": "ls"},
        },
        {
            "type": "result",
            "subtype": "error_max_turns",
            "is_error": True,
            "result": None,
            "session_id": "c1",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0},
            "cost_usd": 0.0,
            "duration_ms": 20,
            "num_turns": 1,
        },
    ]


@pytest.mark.anyio
async def test_tools_blocking_returns_tool_calls(authed_client):
    import json

    runtime = _RecordingRuntime(_tool_events())
    authed_client.app.state.runtime = runtime

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [_BASH_TOOL],
            "stream": False,
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    (call,) = choice["message"]["tool_calls"]
    assert call["id"] == "toolu_1"
    assert call["function"]["name"] == "bash"
    assert json.loads(call["function"]["arguments"]) == {"command": "ls"}

    assert runtime.cfg.max_turns == 1
    assert runtime.cfg.allowed_tools == ["mcp__client__bash"]
    assert runtime.cfg.tools == []
    assert "client" in runtime.cfg.mcp_servers


@pytest.mark.anyio
async def test_tools_streaming_emits_tool_call_chunks_and_usage(authed_client):
    import json

    authed_client.app.state.runtime = _RecordingRuntime(_tool_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={
            "model": "sonnet",
            "messages": [{"role": "user", "content": "list files"}],
            "tools": [_BASH_TOOL],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    chunks = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    tool_deltas = [
        c for c in chunks if c.get("choices") and c["choices"][0]["delta"].get("tool_calls")
    ]
    assert len(tool_deltas) == 1
    call = tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]
    assert call["function"]["name"] == "bash"
    finishes = [c["choices"][0]["finish_reason"] for c in chunks if c.get("choices")]
    assert "tool_calls" in finishes
    usage_chunks = [c for c in chunks if c.get("usage")]
    assert usage_chunks and usage_chunks[0]["usage"]["prompt_tokens"] == 10
    assert r.text.strip().endswith("data: [DONE]")


@pytest.mark.anyio
async def test_tool_history_is_flattened_into_prompt(authed_client):
    runtime = _RecordingRuntime(_events())
    authed_client.app.state.runtime = runtime

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={
            "model": "sonnet",
            "messages": [
                {"role": "system", "content": "You are a CLI agent."},
                {"role": "user", "content": [{"type": "text", "text": "list files"}]},
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
            ],
            "tools": [_BASH_TOOL],
            "stream": False,
        },
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert runtime.cfg.system_prompt is None
    assert "<system_instructions>\nYou are a CLI agent.\n</system_instructions>" in runtime.cfg.prompt
    assert "User: list files" in runtime.cfg.prompt
    assert "Called tools:" in runtime.cfg.prompt
    assert '[toolu_1] bash({"command": "ls"})' in runtime.cfg.prompt
    assert "Tool result [toolu_1]:\nREADME.md" in runtime.cfg.prompt


@pytest.mark.anyio
async def test_chat_completions_model_default_literal_uses_default(authed_client):
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "default", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.json()["model"] == "claude-sonnet-5"


@pytest.mark.anyio
async def test_error_result_blocking_returns_error_envelope(authed_client):
    events = [
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "You're out of extra usage.",
            "session_id": "c1",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            "cost_usd": 0.0,
            "duration_ms": 5,
            "num_turns": 0,
        }
    ]
    authed_client.app.state.runtime = FakeAgentRuntime(events)

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 502
    assert "out of extra usage" in r.json()["error"]["message"]


@pytest.mark.anyio
async def test_error_result_after_text_streaming_emits_error_payload(authed_client):
    import json

    events = [
        {"type": "text", "text": "API Error: 400 out of usage"},
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": None,
            "session_id": "c1",
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            "cost_usd": 0.0,
            "duration_ms": 5,
            "num_turns": 1,
        },
    ]
    authed_client.app.state.runtime = FakeAgentRuntime(events)

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=authed_client.headers,
    )

    chunks = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    errors = [c for c in chunks if "error" in c]
    assert errors and "out of usage" in errors[0]["error"]["message"]
    assert not any(
        c.get("choices") and c["choices"][0].get("finish_reason") == "stop" for c in chunks
    )


@pytest.mark.anyio
async def test_error_event_streaming_emits_error_payload(authed_client):
    import json

    authed_client.app.state.runtime = FakeAgentRuntime([{"type": "error", "message": "boom"}])

    r = await authed_client.client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    chunks = [
        json.loads(line[len("data: "):])
        for line in r.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    errors = [c for c in chunks if "error" in c]
    assert errors and errors[0]["error"]["message"] == "boom"
    assert not any(
        c.get("choices") and c["choices"][0].get("finish_reason") == "stop" for c in chunks
    )
    assert r.text.strip().endswith("data: [DONE]")
