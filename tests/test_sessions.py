import json

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Session, Usage
from tests.conftest import _default_agent_id, _seed_agent, _seed_key
from tests.fakes import FakeAgentRuntime

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Sessions route (from test_sessions_route.py)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_create_session_with_agent_returns_200(authed_client):
    agent_id = await _default_agent_id()
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"].startswith("sess_")
    assert body["profile"] == "default"  # display label = agent name
    assert body["status"] == "active"
    assert "created_at" in body


@pytest.mark.anyio
async def test_create_session_missing_agent_returns_422(authed_client):
    r = await authed_client.client.post(
        "/v1/sessions", json={}, headers=authed_client.headers
    )
    assert r.status_code == 422


@pytest.mark.anyio
async def test_create_session_admin_only_agent_returns_403(authed_client):
    agent_id = await _seed_agent("locked", is_admin_only=True)
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )

    assert r.status_code == 403


@pytest.mark.anyio
async def test_create_session_admin_only_agent_allowed_for_admin_key(authed_client):
    agent_id = await _seed_agent("locked2", is_admin_only=True)
    full_key = await _seed_key(authed_client.tenant_id, "k_admin", is_admin=True)

    r = await authed_client.client.post(
        "/v1/sessions",
        json={"agent": agent_id},
        headers={"Authorization": f"Bearer {full_key}"},
    )

    assert r.status_code == 200
    assert r.json()["profile"] == "locked2"


@pytest.mark.anyio
async def test_create_session_unknown_agent_returns_404(authed_client):
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": "agt_nope"}, headers=authed_client.headers
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_create_session_creates_workspace_dir(authed_client, tmp_path):
    agent_id = await _default_agent_id()
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = r.json()["session_id"]

    from app.services.workspaces import workspace_path

    ws = workspace_path(authed_client.tenant_id, sid)
    assert ws.is_dir()


@pytest.mark.anyio
async def test_list_sessions_returns_only_callers_sessions(authed_client):
    agent_id = await _default_agent_id()
    await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )

    r = await authed_client.client.get("/v1/sessions", headers=authed_client.headers)

    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["profile"] == "default"
    assert body[0]["status"] == "active"


@pytest.mark.anyio
async def test_list_sessions_excludes_other_tenants(authed_client):
    agent_id = await _default_agent_id()
    await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    other_key = await _seed_key("t_other", "k_other", is_admin=False)

    r = await authed_client.client.get(
        "/v1/sessions", headers={"Authorization": f"Bearer {other_key}"}
    )

    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.anyio
async def test_get_session_detail_includes_usage_totals(authed_client):
    agent_id = await _default_agent_id()
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}", headers=authed_client.headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == sid
    assert body["profile"] == "default"
    assert body["agent_id"] == agent_id
    assert body["usage_totals"] == {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "num_runs": 0,
    }


@pytest.mark.anyio
async def test_get_session_detail_sums_usage_rows(authed_client):
    agent_id = await _default_agent_id()
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    from app.db.base import SessionLocal
    from app.db.models import Usage

    async with SessionLocal() as db:
        db.add(
            Usage(
                tenant_id=authed_client.tenant_id,
                api_key_id=authed_client.key_id,
                session_id=sid,
                input_tokens=10,
                output_tokens=20,
                cache_read_tokens=0,
                cost_usd=0.5,
                duration_ms=100,
                num_turns=1,
            )
        )
        db.add(
            Usage(
                tenant_id=authed_client.tenant_id,
                api_key_id=authed_client.key_id,
                session_id=sid,
                input_tokens=5,
                output_tokens=7,
                cache_read_tokens=0,
                cost_usd=0.25,
                duration_ms=50,
                num_turns=1,
            )
        )
        await db.commit()

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}", headers=authed_client.headers
    )

    assert r.status_code == 200
    totals = r.json()["usage_totals"]
    assert totals["cost_usd"] == pytest.approx(0.75)
    assert totals["input_tokens"] == 15
    assert totals["output_tokens"] == 27
    assert totals["num_runs"] == 2


@pytest.mark.anyio
async def test_get_session_not_found_returns_404(authed_client):
    r = await authed_client.client.get(
        "/v1/sessions/sess_doesnotexist", headers=authed_client.headers
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_get_session_owned_by_other_tenant_returns_404(authed_client):
    agent_id = await _default_agent_id()
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]
    other_key = await _seed_key("t_other", "k_other2", is_admin=False)

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {other_key}"}
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_delete_session_sets_status_ended(authed_client):
    agent_id = await _default_agent_id()
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    r = await authed_client.client.delete(
        f"/v1/sessions/{sid}", headers=authed_client.headers
    )

    assert r.status_code == 200
    assert r.json() == {"status": "ended"}

    detail = await authed_client.client.get(
        f"/v1/sessions/{sid}", headers=authed_client.headers
    )
    assert detail.json()["status"] == "ended"


@pytest.mark.anyio
async def test_delete_session_owned_by_other_tenant_returns_404(authed_client):
    agent_id = await _default_agent_id()
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": agent_id}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]
    other_key = await _seed_key("t_other", "k_other3", is_admin=False)

    r = await authed_client.client.delete(
        f"/v1/sessions/{sid}", headers={"Authorization": f"Bearer {other_key}"}
    )

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Messages route (from test_messages_route.py)
# ---------------------------------------------------------------------------


def _events() -> list[dict]:
    return [
        {"type": "init", "session_id": "sdk-1"},
        {"type": "text", "text": "hi"},
        {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "session_id": "sdk-1",
            "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0},
            "cost_usd": 0.01,
            "duration_ms": 100,
            "num_turns": 1,
        },
    ]


async def _create_session(authed_client) -> str:
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    assert r.status_code == 200
    return r.json()["session_id"]


@pytest.mark.anyio
async def test_send_message_blocking_returns_result_and_persists_usage(authed_client):
    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "result"
    assert body["result"] == "done"
    assert body["cost_usd"] == pytest.approx(0.01)

    async with SessionLocal() as db:
        session = await db.get(Session, sid)
        assert session.sdk_session_id == "sdk-1"
        assert session.status == "active"

        usage_rows = (
            (await db.execute(select(Usage).where(Usage.session_id == sid)))
            .scalars()
            .all()
        )
        assert len(usage_rows) == 1
        assert usage_rows[0].input_tokens == 10
        assert usage_rows[0].output_tokens == 5
        assert usage_rows[0].cost_usd == pytest.approx(0.01)


@pytest.mark.anyio
async def test_send_message_streaming_returns_sse(authed_client):
    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello", "stream": True},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: text" in r.text
    assert "event: result" in r.text
    assert "event: init" in r.text


@pytest.mark.anyio
async def test_send_message_while_running_returns_409(authed_client):
    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    async with SessionLocal() as db:
        session = await db.get(Session, sid)
        session.status = "running"
        await db.commit()

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 409


@pytest.mark.anyio
async def test_get_message_history_returns_ordered_events(authed_client):
    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}/messages", headers=authed_client.headers
    )

    assert r.status_code == 200
    body = r.json()
    # The user's prompt is persisted first, then the agent's init/text/result.
    assert [e["type"] for e in body] == ["user_message", "init", "text", "result"]
    assert [e["seq"] for e in body] == [1, 2, 3, 4]
    assert body[0]["payload"]["text"] == "hello"
    assert body[1]["payload"]["session_id"] == "sdk-1"


@pytest.mark.anyio
async def test_send_message_build_config_failure_resets_session_and_does_not_leak_gate(
    authed_client,
):
    """Regression test for the run-gate leak + stuck-session bug.

    build_run_config (called via resolve_mcp) raises when a session
    references an MCP server name that doesn't resolve (e.g. a deleted MCP
    server). Previously the run_gate was acquired *before* build_run_config
    ran, so a failure here left the gate slot held forever and the session
    stuck in status="running". This asserts: a 4xx is returned, the session
    is reset back to "active", the gate isn't leaked (in_flight() == 0), and
    a subsequent normal request on a fresh session still succeeds.
    """
    from app.services import ratelimit

    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    # Back the session with an agent whose MCP name doesn't resolve to
    # anything, forcing build_run_config -> resolve_mcp to raise.
    from app.services.agents import create_agent

    async with SessionLocal() as db:
        bad_agent = await create_agent(
            db,
            name="badmcp",
            model="claude-sonnet-5",
            allowed_tools=["Read"],
            mcp_names=["nonexistent-mcp-server"],
        )
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": bad_agent.id}, headers=authed_client.headers
    )
    sid = r.json()["session_id"]

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )

    assert 400 <= r.status_code < 500

    # Gate slot was never leaked (acquire happens after build_run_config).
    assert ratelimit.run_gate.in_flight() == 0

    async with SessionLocal() as db:
        session = await db.get(Session, sid)
        assert session.status == "active"

    # A fresh session's request still succeeds afterwards.
    sid2 = await _create_session(authed_client)
    r2 = await authed_client.client.post(
        f"/v1/sessions/{sid2}/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )
    assert r2.status_code == 200
    assert ratelimit.run_gate.in_flight() == 0


@pytest.mark.anyio
async def test_send_message_not_found_returns_404(authed_client):
    r = await authed_client.client.post(
        "/v1/sessions/sess_doesnotexist/messages",
        json={"prompt": "hello", "stream": False},
        headers=authed_client.headers,
    )

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Files route (from test_files_route.py)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_list_download_roundtrip(authed_client):
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    upload = await authed_client.client.post(
        f"/v1/sessions/{sid}/files",
        headers=authed_client.headers,
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )

    assert upload.status_code == 200
    body = upload.json()
    assert body["path"] == "hello.txt"
    assert body["size"] == len(b"hello world")

    listing = await authed_client.client.get(
        f"/v1/sessions/{sid}/files", headers=authed_client.headers
    )
    assert listing.status_code == 200
    assert listing.json() == [{"path": "hello.txt", "size": len(b"hello world")}]

    download = await authed_client.client.get(
        f"/v1/sessions/{sid}/files/hello.txt", headers=authed_client.headers
    )
    assert download.status_code == 200
    assert download.content == b"hello world"


@pytest.mark.anyio
async def test_download_path_escape_returns_422(authed_client):
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    # Percent-encode the dot segments so httpx sends them literally instead of
    # normalizing "../.." away client-side before the request is issued.
    r = await authed_client.client.get(
        f"/v1/sessions/{sid}/files/%2e%2e/%2e%2e/etc/passwd",
        headers=authed_client.headers,
    )

    assert r.status_code == 422


@pytest.mark.anyio
async def test_download_missing_file_returns_404(authed_client):
    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}/files/nope.txt", headers=authed_client.headers
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_files_scoped_to_owning_tenant(authed_client):
    from tests.conftest import _seed_key

    created = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    sid = created.json()["session_id"]
    other_key = await _seed_key("t_other", "k_other_files", is_admin=False)

    r = await authed_client.client.get(
        f"/v1/sessions/{sid}/files", headers={"Authorization": f"Bearer {other_key}"}
    )

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Admin session actions (from test_session_actions_admin.py)
# ---------------------------------------------------------------------------


async def _login(client):
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code in (302, 303)


async def _make_session(tenant_id: str) -> str:
    """Create an agent-backed session directly and return its id."""
    from app.db.base import SessionLocal
    from app.services.sessions import create_session_record

    agent_id = await _default_agent_id()
    async with SessionLocal() as db:
        session = await create_session_record(
            db, tenant_id=tenant_id, agent_id=agent_id, allow_admin_only=True
        )
        return session.id


async def test_upload_list_and_download_file_via_dashboard(client):
    await _seed_key("t_files", "k_files")  # seeds the tenant + a key
    await _login(client)
    sid = await _make_session("t_files")

    # upload
    r = await client.post(
        f"/admin/sessions/{sid}/files",
        files={"file": ("notes.txt", b"hello from upload", "text/plain")},
    )
    assert r.status_code in (302, 303)

    # the detail page lists the file
    detail = await client.get(f"/admin/sessions/{sid}")
    assert "notes.txt" in detail.text

    # and it downloads with the right contents
    dl = await client.get(f"/admin/sessions/{sid}/files/notes.txt")
    assert dl.status_code == 200
    assert dl.content == b"hello from upload"


async def test_delete_file_via_dashboard(client):
    await _seed_key("t_del", "k_del")
    await _login(client)
    sid = await _make_session("t_del")

    await client.post(
        f"/admin/sessions/{sid}/files",
        files={"file": ("gone.txt", b"bye", "text/plain")},
    )
    assert "gone.txt" in (await client.get(f"/admin/sessions/{sid}")).text

    r = await client.delete(f"/admin/sessions/{sid}/files/gone.txt")
    assert r.status_code == 200

    assert "gone.txt" not in (await client.get(f"/admin/sessions/{sid}")).text
    assert (await client.get(f"/admin/sessions/{sid}/files/gone.txt")).status_code == 404


async def test_download_path_escape_rejected(client):
    await _seed_key("t_files2", "k_files2")
    await _login(client)
    sid = await _make_session("t_files2")

    r = await client.get(f"/admin/sessions/{sid}/files/%2e%2e/%2e%2e/etc/passwd")
    assert r.status_code in (404, 422)


async def test_delete_session_removes_row_events_and_workspace(client):
    await _seed_key("t_delsess", "k_delsess")
    await _login(client)
    sid = await _make_session("t_delsess")

    # give it a file + an event so we exercise the cascade paths
    await client.post(
        f"/admin/sessions/{sid}/files", files={"file": ("a.txt", b"hi", "text/plain")}
    )
    from pathlib import Path
    from app.db.base import SessionLocal
    from app.db.models import Event, Session

    async with SessionLocal() as db:
        db.add(Event(session_id=sid, seq=1, type="text", payload_json="{}"))
        await db.commit()
        ws = (await db.get(Session, sid)).workspace_path
    assert Path(ws).is_dir()

    r = await client.post(f"/admin/sessions/{sid}/delete")
    assert r.status_code in (302, 303)

    async with SessionLocal() as db:
        from sqlalchemy import select

        assert await db.get(Session, sid) is None
        rows = (await db.execute(select(Event).where(Event.session_id == sid))).scalars().all()
        assert rows == []
    assert not Path(ws).exists()  # workspace removed


async def test_send_message_without_tenant_key_returns_422(client):
    # A tenant with NO api key: the message route can't attribute usage.
    from app.db.base import SessionLocal
    from app.db.models import Tenant

    async with SessionLocal() as db:
        db.add(Tenant(id="t_nokey", name="t_nokey"))
        await db.commit()

    await _login(client)
    sid = await _make_session("t_nokey")

    r = await client.post(f"/admin/sessions/{sid}/message", json={"prompt": "hi"})
    assert r.status_code == 422


async def test_send_message_empty_prompt_returns_422(client):
    await _seed_key("t_files3", "k_files3")
    await _login(client)
    sid = await _make_session("t_files3")

    r = await client.post(f"/admin/sessions/{sid}/message", json={"prompt": "   "})
    assert r.status_code == 422
