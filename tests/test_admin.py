import pytest

from app.services import claude_cli
from tests.conftest import _seed_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def admin_client(client):
    """An httpx client plus a seeded admin API key."""
    full_key = await _seed_key("t_admin", "k_admin", is_admin=True)
    return {"client": client, "headers": {"Authorization": f"Bearer {full_key}"}}


@pytest.mark.anyio
async def test_admin_flow_end_to_end(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]

    # create tenant
    r = await c.post("/admin/api/tenants", json={"name": "Acme"}, headers=h)
    assert r.status_code == 200, r.text
    tenant = r.json()
    assert tenant["name"] == "Acme"
    tenant_id = tenant["id"]
    assert tenant_id

    # create a key for that tenant
    r = await c.post(
        "/admin/api/keys",
        json={
            "tenant_id": tenant_id,
            "name": "acme-key",
            "rpm": 20,
            "daily_cost_usd": 5.0,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["api_key"].startswith("cak_")
    assert created["prefix"] == created["api_key"][:12]
    assert "id" in created
    key_id = created["id"]
    full_key = created["api_key"]

    # the newly created key works for authenticated (non-admin) requests
    r = await c.get("/v1/usage", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 200

    # revoke it
    r = await c.post(f"/admin/api/keys/{key_id}/revoke", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["revoked_at"] is not None

    # revoked key no longer authenticates
    r = await c.get("/v1/usage", headers={"Authorization": f"Bearer {full_key}"})
    assert r.status_code == 401

    # PATCH limits on a fresh (non-revoked) key
    r = await c.post(
        "/admin/api/keys",
        json={
            "tenant_id": tenant_id,
            "name": "acme-key-2",
            "rpm": 20,
            "daily_cost_usd": 5.0,
        },
        headers=h,
    )
    key_id_2 = r.json()["id"]
    r = await c.patch(
        f"/admin/api/keys/{key_id_2}",
        json={"rpm": 99, "daily_cost_usd": 42.0},
        headers=h,
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["rpm"] == 99
    assert patched["daily_cost_usd"] == 42.0

    # GET /keys never leaks key_hash / full key
    r = await c.get("/admin/api/keys", headers=h)
    assert r.status_code == 200
    keys = r.json()
    assert len(keys) >= 2
    for k in keys:
        assert "key_hash" not in k
        assert "api_key" not in k
        assert set(k.keys()) == {
            "id",
            "prefix",
            "tenant_id",
            "name",
            "rpm",
            "daily_cost_usd",
            "is_admin",
            "revoked_at",
            "created_at",
        }

    # PUT billing config
    r = await c.put(
        f"/admin/api/billing/{tenant_id}",
        json={
            "price_per_mtok_input": 3.0,
            "price_per_mtok_output": 15.0,
            "markup": 0.2,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    billing = r.json()
    assert billing["tenant_id"] == tenant_id
    assert billing["markup"] == 0.2

    # upsert again (update, not duplicate)
    r = await c.put(
        f"/admin/api/billing/{tenant_id}",
        json={
            "price_per_mtok_input": 4.0,
            "price_per_mtok_output": 20.0,
            "markup": 0.1,
        },
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["price_per_mtok_input"] == 4.0

    # seed some usage for this tenant and check aggregation + pricing
    from app.db.base import SessionLocal
    from app.db.models import Usage

    async with SessionLocal() as db:
        db.add(
            Usage(
                tenant_id=tenant_id,
                api_key_id=key_id_2,
                session_id="sess_x",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                cache_read_tokens=0,
                cost_usd=1.23,
                duration_ms=10,
                num_turns=1,
            )
        )
        await db.commit()

    r = await c.get("/admin/api/usage", headers=h)
    assert r.status_code == 200, r.text
    rows = {row["tenant_id"]: row for row in r.json()}
    assert tenant_id in rows
    row = rows[tenant_id]
    assert row["runs"] == 1
    assert row["input_tokens"] == 1_000_000
    assert row["output_tokens"] == 1_000_000
    assert row["cost_usd"] == pytest.approx(1.23)
    # priced via billing config: 1*4.0 + 1*20.0 = 24.0, * (1 + 0.1) = 26.4
    assert row["priced_cost_usd"] == pytest.approx(26.4)


@pytest.mark.anyio
async def test_admin_endpoints_reject_non_admin_key(authed_client):
    c = authed_client.client
    h = authed_client.headers

    r = await c.post("/admin/api/tenants", json={"name": "Nope"}, headers=h)
    assert r.status_code == 403

    r = await c.post(
        "/admin/api/keys",
        json={
            "tenant_id": authed_client.tenant_id,
            "name": "x",
            "rpm": 1,
            "daily_cost_usd": 1.0,
        },
        headers=h,
    )
    assert r.status_code == 403

    r = await c.post(f"/admin/api/keys/{authed_client.key_id}/revoke", headers=h)
    assert r.status_code == 403

    r = await c.patch(
        f"/admin/api/keys/{authed_client.key_id}", json={"rpm": 5}, headers=h
    )
    assert r.status_code == 403

    r = await c.get("/admin/api/keys", headers=h)
    assert r.status_code == 403

    r = await c.put(
        f"/admin/api/billing/{authed_client.tenant_id}",
        json={"price_per_mtok_input": 1.0, "price_per_mtok_output": 1.0, "markup": 0.0},
        headers=h,
    )
    assert r.status_code == 403

    r = await c.get("/admin/api/usage", headers=h)
    assert r.status_code == 403


@pytest.mark.anyio
async def test_admin_endpoints_reject_missing_auth(client):
    r = await client.post("/admin/api/tenants", json={"name": "Nope"})
    assert r.status_code == 401


@pytest.mark.anyio
async def test_create_key_for_unknown_tenant_returns_404(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]
    r = await c.post(
        "/admin/api/keys",
        json={"tenant_id": "ten_missing", "name": "x", "rpm": 1, "daily_cost_usd": 1.0},
        headers=h,
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_revoke_unknown_key_returns_404(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]
    r = await c.post("/admin/api/keys/key_missing/revoke", headers=h)
    assert r.status_code == 404


@pytest.mark.anyio
async def test_bootstrap_admin_creates_key_when_env_set(tmp_path, monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'boot.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    monkeypatch.setenv("BOOTSTRAP_ADMIN", "1")

    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.db.base as base

    base.reset_engine()

    app = create_app()
    async with app.router.lifespan_context(app):
        from sqlalchemy import select

        from app.db.base import SessionLocal
        from app.db.models import ApiKey

        async with SessionLocal() as db:
            row = (
                await db.execute(select(ApiKey).where(ApiKey.is_admin.is_(True)))
            ).scalar_one_or_none()
            assert row is not None
            assert row.revoked_at is None

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_bootstrap_admin_is_idempotent(tmp_path, monkeypatch):
    """A second bootstrap run must not create a second admin key."""
    from app.db.base import SessionLocal
    from app.db.models import ApiKey
    from app.services.admin import bootstrap_admin_if_needed

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'boot2.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.db.base as base

    base.reset_engine()
    await base.init_db()

    async with SessionLocal() as db:
        first = await bootstrap_admin_if_needed(db)
    assert first is not None

    async with SessionLocal() as db:
        second = await bootstrap_admin_if_needed(db)
    assert second is None

    async with SessionLocal() as db:
        from sqlalchemy import func, select

        count = (
            await db.execute(
                select(func.count()).select_from(ApiKey).where(ApiKey.is_admin.is_(True))
            )
        ).scalar_one()
    assert count == 1

    get_settings.cache_clear()


@pytest.mark.anyio
async def test_keys_page_redirects_when_no_cookie(client):
    r = await client.get("/admin/keys")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


@pytest.mark.anyio
async def test_login_wrong_password_shows_error_and_sets_no_cookie(client):
    r = await client.post("/admin/login", data={"password": "wrong"})
    assert r.status_code == 200
    assert "admin_session" not in r.cookies
    assert "incorrect" in r.text.lower() or "invalid" in r.text.lower()


@pytest.mark.anyio
async def test_login_then_keys_table_shows_seeded_key(client):
    full_key = await _seed_key("t_ui", "k_ui", is_admin=False)
    prefix = full_key[:12]

    # correct password logs in and sets the signed cookie
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302
    assert "admin_session" in r.cookies

    # cookie now grants access to the keys page
    r = await client.get("/admin/keys")
    assert r.status_code == 200
    assert "<table" in r.text
    assert prefix in r.text


@pytest.mark.anyio
async def test_logout_clears_cookie_and_keys_redirects_again(client):
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.get("/admin/logout")
    assert r.status_code == 302

    r = await client.get("/admin/keys")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


@pytest.mark.anyio
async def test_remaining_pages_render_when_authed(client):
    await _seed_key("t_ui2", "k_ui2", is_admin=False)
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    for path in ("/admin", "/admin/billing", "/admin/usage", "/admin/sessions", "/admin/system"):
        r = await client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}"


@pytest.mark.anyio
async def test_create_and_revoke_key_via_form(client):
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.post("/admin/tenants", data={"name": "Acme"})
    assert r.status_code == 302

    r = await client.get("/admin/keys")
    assert "Acme" in r.text
    import re

    m = re.search(r"value=\"(ten_[0-9a-f]+)\"", r.text)
    assert m, r.text
    tenant_id = m.group(1)

    r = await client.post(
        "/admin/keys",
        data={
            "tenant_id": tenant_id,
            "name": "form-key",
            "rpm": 10,
            "daily_cost_usd": 5.0,
        },
    )
    # Creating a key renders the keys page with the full key revealed exactly
    # once (with a copy control) instead of redirecting — so the operator can
    # copy it before it becomes unrecoverable.
    assert r.status_code == 200
    assert "form-key" in r.text
    assert "key-reveal" in r.text  # the one-time full-key banner
    assert 'id="newkey"' in r.text  # the copyable full-key input

    r = await client.get("/admin/keys")
    assert "form-key" in r.text
    assert "key-reveal" not in r.text  # not shown again on a plain reload

    import re as re2

    m2 = re2.search(r"/admin/keys/(key_[0-9a-f]+)/revoke", r.text)
    assert m2, r.text
    key_id = m2.group(1)

    r = await client.post(f"/admin/keys/{key_id}/revoke")
    assert r.status_code == 302

    r = await client.get("/admin/keys")
    assert r.status_code == 200


@pytest.mark.anyio
async def test_billing_form_submit_and_session_drilldown(client):
    from app.db.base import SessionLocal
    from app.db.models import Event
    from app.db.models import Session as SessionModel
    from app.db.models import Tenant

    async with SessionLocal() as db:
        db.add(Tenant(id="ten_bill", name="Billed Co"))
        db.add(
            SessionModel(
                id="sess_ui1",
                tenant_id="ten_bill",
                profile="default",
                overrides_json="{}",
                mcp_names_json="[]",
                workspace_path="/tmp/ws",
                status="ended",
            )
        )
        db.add(Event(session_id="sess_ui1", seq=1, type="message", payload_json="{}"))
        await db.commit()

    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.post(
        "/admin/billing",
        data={
            "tenant_id": "ten_bill",
            "price_per_mtok_input": 3.0,
            "price_per_mtok_output": 15.0,
            "markup": 0.1,
        },
    )
    assert r.status_code == 302

    r = await client.get("/admin/billing")
    assert r.status_code == 200
    assert "ten_bill" in r.text
    assert "billing.upsert" in r.text  # cost log entry

    r = await client.get("/admin/sessions")
    assert r.status_code == 200
    assert "sess_ui1" in r.text

    r = await client.get("/admin/sessions/sess_ui1")
    assert r.status_code == 200
    assert "message" in r.text  # event type shown


async def test_status_not_available(monkeypatch):
    monkeypatch.setattr(claude_cli, "_cli_path", lambda: None)
    s = await claude_cli.cli_status()
    assert s == {"installed": False, "logged_in": False, "ready": False, "bundled": False}


async def test_status_parses_logged_in(monkeypatch):
    monkeypatch.setattr(claude_cli, "_cli_path", lambda: "/pkg/_bundled/claude")

    async def fake_run(*args, timeout=15.0):
        if "--version" in args:
            return 0, "2.1.0 (Claude Code)", ""
        if "status" in args:
            return 0, '{"loggedIn": true, "email": "a@b.c", "subscriptionType": "max"}', ""
        return 1, "", ""

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    s = await claude_cli.cli_status()
    assert s["installed"] and s["logged_in"] and s["ready"]
    assert s["email"] == "a@b.c" and s["plan"] == "max"
    assert s["bundled"] is True and s["version"] == "2.1.0"


async def test_status_not_logged_in(monkeypatch):
    monkeypatch.setattr(claude_cli, "_cli_path", lambda: "/usr/bin/claude")

    async def fake_run(*args, timeout=15.0):
        if "--version" in args:
            return 0, "2.1.0", ""
        if "status" in args:
            return 0, '{"loggedIn": false}', ""
        return 1, "", ""

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    s = await claude_cli.cli_status()
    assert s["installed"] and not s["logged_in"] and not s["ready"]
    assert s["bundled"] is False


async def test_submit_login_code_no_process(monkeypatch):
    monkeypatch.setattr(claude_cli, "_login_proc", None)
    r = await claude_cli.submit_login_code("abc")
    assert r["ok"] is False and "sign-in" in r["error"].lower()


async def test_submit_login_code_success(monkeypatch):
    class FakeStdin:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    class FakeProc:
        returncode = None
        stdin = FakeStdin()

        async def communicate(self, *a, **k):
            return b"", b""

    monkeypatch.setattr(claude_cli, "_login_proc", FakeProc())

    async def fake_status():
        return {"installed": True, "logged_in": True, "ready": True, "email": "a@b.c", "plan": "max"}

    monkeypatch.setattr(claude_cli, "cli_status", fake_status)
    r = await claude_cli.submit_login_code("the-code#state")
    assert r["ok"] is True and r["status"]["logged_in"] is True
    assert claude_cli._login_proc is None


async def test_onboarding_login_code_route(client, monkeypatch):
    async def fake_submit(code):
        assert code == "pasted-code"
        return {"ok": True, "status": {"logged_in": True}}

    monkeypatch.setattr(claude_cli, "submit_login_code", fake_submit)
    await client.post("/admin/login", data={"password": "admin"})
    r = await client.post("/admin/onboarding/login/code", json={"code": "pasted-code"})
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_logout(monkeypatch):
    monkeypatch.setattr(claude_cli, "_cli_path", lambda: "/pkg/_bundled/claude")
    monkeypatch.setattr(claude_cli, "_login_proc", None)

    async def fake_run(*args, timeout=15.0):
        assert "logout" in args
        return 0, "Signed out", ""

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    r = await claude_cli.logout()
    assert r["ok"] is True


async def test_onboarding_logout_route(client, monkeypatch):
    async def fake_logout():
        return {"ok": True}

    monkeypatch.setattr(claude_cli, "logout", fake_logout)
    await client.post("/admin/login", data={"password": "admin"})
    r = await client.post("/admin/onboarding/logout")
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_setup_on_system_page_and_status(client, monkeypatch):
    ready = {
        "installed": True, "bundled": True, "version": "2.1", "path": "/x/_bundled/claude",
        "logged_in": True, "email": "e@x.io", "plan": "max", "ready": True,
    }

    async def fake_status():
        return ready

    monkeypatch.setattr(claude_cli, "cli_status", fake_status)
    monkeypatch.setattr(claude_cli, "cli_status_cached", fake_status)

    await client.post("/admin/login", data={"password": "admin"})

    # Setup now lives on the System page.
    r = await client.get("/admin/system")
    assert r.status_code == 200
    assert "Claude runtime" in r.text and "Signed in to Claude" in r.text

    # The old Setup path redirects there.
    r_redir = await client.get("/admin/onboarding", follow_redirects=False)
    assert r_redir.status_code == 307
    assert r_redir.headers["location"] == "/admin/system"

    r2 = await client.get("/admin/onboarding/status")
    assert r2.status_code == 200
    assert r2.json()["ready"] is True
