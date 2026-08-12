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
    assert ids == {"opus", "sonnet", "haiku"}


@pytest.mark.anyio
async def test_chat_completions_missing_key_returns_401(client):
    r = await client.post(
        "/v1/chat/completions",
        json={"model": "sonnet", "messages": [{"role": "user", "content": "hi"}], "stream": False},
    )
    assert r.status_code == 401
