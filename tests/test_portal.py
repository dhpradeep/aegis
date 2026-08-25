import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Agent, Event, Session
from tests.conftest import _seed_key
from tests.fakes import FakeAgentRuntime
from tests.test_openai_compat import _RecordingRuntime, _events


async def _chat_agent_id() -> str:
    async with SessionLocal() as db:
        return (await db.execute(select(Agent).where(Agent.name == "chat"))).scalar_one().id


async def _default_agent_id() -> str:
    async with SessionLocal() as db:
        return (await db.execute(select(Agent).where(Agent.name == "default"))).scalar_one().id


async def _login(authed_client):
    token = authed_client.headers["Authorization"].split(" ", 1)[1]
    r = await authed_client.client.post("/chat/login", data={"api_key": token})
    assert r.status_code == 302 and r.headers["location"] == "/chat"


@pytest.mark.anyio
async def test_portal_requires_login(client):
    r = await client.get("/chat")
    assert r.status_code == 302 and r.headers["location"] == "/chat/login"


@pytest.mark.anyio
async def test_portal_login_rejects_bad_key(client):
    r = await client.post("/chat/login", data={"api_key": "cak_nope"})
    assert r.status_code == 200 and "Unknown or revoked" in r.text
    assert (await client.get("/chat")).status_code == 302


@pytest.mark.anyio
async def test_portal_chat_flow(authed_client):
    c = authed_client.client
    authed_client.app.state.runtime = FakeAgentRuntime(_events())
    await _login(authed_client)

    r = await c.get("/chat")
    assert r.status_code == 200 and "New chat" in r.text and "No chats yet" in r.text

    chat_agent = await _chat_agent_id()
    r = await c.post(
        "/chat/sessions",
        data={"agent_id": chat_agent, "title": "Hello", "model": "claude-opus-5", "effort": "high"},
    )
    assert r.status_code == 302
    sid = r.headers["location"].rsplit("/", 1)[1]
    async with SessionLocal() as db:
        assert json.loads((await db.get(Session, sid)).overrides_json) == {"model": "claude-opus-5", "effort": "high"}

    r = await c.get(f"/chat/s/{sid}")
    assert r.status_code == 200 and 'data-base="/chat/s/' in r.text and "Hello" in r.text

    runtime = _RecordingRuntime(_events())
    authed_client.app.state.runtime = runtime
    r = await c.post(f"/chat/s/{sid}/message", json={"prompt": "hi"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "event: result" in r.text
    assert runtime.cfg.model == "claude-opus-5" and runtime.cfg.effort == "high"
    assert runtime.cfg.allowed_tools == ["WebSearch"]

    r = await c.post(f"/chat/s/{sid}/settings", data={"model": "not-a-model", "effort": "low"})
    assert r.status_code == 302
    async with SessionLocal() as db:
        assert json.loads((await db.get(Session, sid)).overrides_json) == {"effort": "low"}

    async with SessionLocal() as db:
        types = [
            e.type
            for e in (await db.execute(select(Event).where(Event.session_id == sid).order_by(Event.seq))).scalars().all()
        ]
        session = await db.get(Session, sid)
    assert types[:2] == ["user_message", "init"] and "result" in types
    assert session.status == "active"

    r = await c.get(f"/chat/s/{sid}/state")
    assert r.status_code == 200 and r.json()["status"] == "active"

    r = await c.get(f"/chat/s/{sid}")
    assert r.status_code == 200 and "hi" in r.text
    async with SessionLocal() as db:
        assert (await db.get(Session, sid)).origin == "portal"

    r = await c.get("/chat/usage")
    assert r.status_code == 200 and "you" in r.text

    r = await c.post(f"/chat/s/{sid}/delete")
    assert r.status_code == 302
    async with SessionLocal() as db:
        assert await db.get(Session, sid) is None


@pytest.mark.anyio
async def test_portal_rejects_non_portal_agent_and_foreign_session(authed_client):
    c = authed_client.client
    await _login(authed_client)

    r = await c.post("/chat/sessions", data={"agent_id": await _default_agent_id(), "title": "x"})
    assert r.status_code == 403

    other_key = await _seed_key("t_other", "k_other")
    async with SessionLocal() as db:
        from app.services.sessions import create_session_record

        foreign = await create_session_record(db, tenant_id="t_other", agent_id=await _chat_agent_id(), title="theirs")
    assert (await c.get(f"/chat/s/{foreign.id}")).status_code == 404
    assert (await c.post(f"/chat/s/{foreign.id}/message", json={"prompt": "hi"})).status_code == 404

    r = await c.get("/chat")
    assert "theirs" not in r.text


@pytest.mark.anyio
async def test_portal_hides_cli_and_api_sessions(authed_client):
    c = authed_client.client
    await _login(authed_client)
    async with SessionLocal() as db:
        from app.api.compat.conversation import create_cli_session
        from app.services.sessions import create_session_record

        cli = await create_cli_session(db, authed_client.tenant_id, "claude-code", "from cli")
        api = await create_session_record(db, tenant_id=authed_client.tenant_id, agent_id=await _chat_agent_id(), title="from api")
    r = await c.get("/chat")
    assert "from cli" not in r.text and "from api" not in r.text
    assert (await c.get(f"/chat/s/{cli.id}")).status_code == 404
    assert (await c.get(f"/chat/s/{api.id}")).status_code == 404


@pytest.mark.anyio
async def test_portal_logout(authed_client):
    c = authed_client.client
    await _login(authed_client)
    assert (await c.get("/chat")).status_code == 200
    r = await c.get("/chat/logout")
    assert r.status_code == 302
    assert (await c.get("/chat")).status_code == 302


@pytest.mark.anyio
async def test_portal_login_is_throttled(client):
    saw_lockout = False
    for _ in range(12):
        r = await client.post("/chat/login", data={"api_key": "cak_wrong"})
        if r.status_code == 429 or "Too many attempts" in r.text:
            saw_lockout = True
            break
    assert saw_lockout
    r = await client.post("/chat/login", data={"api_key": "cak_wrong"})
    assert r.status_code == 429


@pytest.mark.anyio
async def test_portal_chat_enforces_key_rpm(authed_client):
    from app.db.models import ApiKey

    c = authed_client.client
    authed_client.app.state.runtime = FakeAgentRuntime(_events())
    await _login(authed_client)
    async with SessionLocal() as db:
        (await db.get(ApiKey, authed_client.key_id)).rpm = 1
        await db.commit()

    r = await c.post("/chat/sessions", data={"agent_id": await _chat_agent_id()})
    sid = r.headers["location"].rsplit("/", 1)[1]
    assert (await c.post(f"/chat/s/{sid}/message", json={"prompt": "one"})).status_code == 200
    r = await c.post(f"/chat/s/{sid}/message", json={"prompt": "two"})
    assert r.status_code == 429
    async with SessionLocal() as db:
        assert (await db.get(Session, sid)).status == "active"


@pytest.mark.anyio
async def test_landing_page_links_admin_and_chat(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert 'href="/chat"' in r.text and 'href="/admin"' in r.text
