import socket
import sqlite3

import pytest
from fastapi import FastAPI, Depends
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import cli
from app.api.deps import require_key
from app.core.errors import ApiError, install_error_handlers
from app.core.logging import RequestIDMiddleware
from app.core.security import generate_api_key, hash_key
from app.db.base import init_db, SessionLocal, ensure_sqlite_dir
from app.db.models import Tenant, ApiKey


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --- from tests/test_db.py ---------------------------------------------------


@pytest.mark.anyio
async def test_create_and_read_tenant(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'d.db'}")
    from app.core.config import get_settings; get_settings.cache_clear()
    import app.db.base as base; base.reset_engine()  # rebuild engine for the new URL
    await init_db()
    async with SessionLocal() as db:
        db.add(Tenant(id="t1", name="Acme"))
        db.add(ApiKey(id="k1", tenant_id="t1", key_hash="h", prefix="cak_abc",
                      name="default", rpm=30, daily_cost_usd=10.0, is_admin=False))
        await db.commit()
    async with SessionLocal() as db:
        row = (await db.execute(select(ApiKey).where(ApiKey.prefix == "cak_abc"))).scalar_one()
        assert row.tenant_id == "t1"


# --- from tests/test_db_bootstrap.py -----------------------------------------


def test_ensure_sqlite_dir_creates_missing_parent(tmp_path):
    nested = tmp_path / "a" / "b" / "app.db"
    assert not nested.parent.exists()
    ensure_sqlite_dir(f"sqlite+aiosqlite:///{nested}")
    assert nested.parent.is_dir()


def test_ensure_sqlite_dir_ignores_memory_and_non_sqlite():
    # Must not raise for in-memory or non-file / non-sqlite URLs.
    ensure_sqlite_dir("sqlite+aiosqlite:///:memory:")
    ensure_sqlite_dir("postgresql+asyncpg://user:pw@host/db")


# --- from tests/test_migrations.py -------------------------------------------


@pytest.mark.anyio
async def test_migrations_build_full_schema(tmp_path, monkeypatch):
    """`alembic upgrade head` builds the complete schema on a fresh DB.

    Guards the migration file + env against drift: every mapped table (and the
    alembic_version bookkeeping table) must exist after upgrading from empty.
    """
    db_path = tmp_path / "mig.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.db.migrate import run_migrations

    await run_migrations()

    con = sqlite3.connect(db_path)
    try:
        tables = {
            r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        session_cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    finally:
        con.close()

    # sessions.agent_id (a later model addition) must be present via the migration.
    assert "agent_id" in session_cols

    expected = {
        "agents",
        "objectives",
        "sessions",
        "api_keys",
        "tenants",
        "completion_logs",
        "events",
        "usage",
        "mcp_servers",
        "jobs",
        "webhook_configs",
        "billing_configs",
        "rate_buckets",
        "audit_logs",
        "alembic_version",
    }
    missing = expected - tables
    assert not missing, f"migration did not create: {missing}"


# --- from tests/test_errors.py -----------------------------------------------


def _app():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)
    install_error_handlers(app)
    @app.get("/boom")
    async def boom():
        raise ApiError.not_found("no such thing")
    @app.get("/rl")
    async def rl():
        raise ApiError.rate_limited(retry_after=42)
    return app

@pytest.mark.anyio
async def test_error_envelope():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/boom")
        assert r.status_code == 404
        err = r.json()["error"]
        assert err["type"] == "not_found"
        assert err["message"] == "no such thing"
        assert err["request_id"]

@pytest.mark.anyio
async def test_rate_limit_retry_after():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get("/rl")
        assert r.status_code == 429
        assert r.headers["retry-after"] == "42"


# --- from tests/test_auth.py -------------------------------------------------


def test_key_generation_roundtrip():
    full, prefix, h = generate_api_key()
    assert full.startswith("cak_")
    assert prefix == full[:12]
    assert h == hash_key(full)
    assert len(h) == 64


def _protected_app():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/protected")
    async def protected(key: ApiKey = Depends(require_key)):
        return {"tenant_id": key.tenant_id}

    return app


@pytest.mark.anyio
async def test_require_key_rejects_missing_header(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'auth.db'}")
    from app.core.config import get_settings
    get_settings.cache_clear()
    import app.db.base as base
    base.reset_engine()
    await init_db()

    app = _protected_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/protected")
        assert r.status_code == 401


@pytest.mark.anyio
async def test_require_key_accepts_valid_key(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'auth2.db'}")
    from app.core.config import get_settings
    get_settings.cache_clear()
    import app.db.base as base
    base.reset_engine()
    await init_db()

    full_key, prefix, key_hash = generate_api_key()
    async with SessionLocal() as db:
        db.add(Tenant(id="t1", name="Acme"))
        db.add(ApiKey(
            id="k1", tenant_id="t1", key_hash=key_hash, prefix=prefix,
            name="default", rpm=30, daily_cost_usd=10.0, is_admin=False,
        ))
        await db.commit()

    app = _protected_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/protected", headers={"Authorization": f"Bearer {full_key}"})
        assert r.status_code == 200
        assert r.json() == {"tenant_id": "t1"}


# --- from tests/test_cli.py --------------------------------------------------


def test_port_available_true_for_free_port():
    # Bind an ephemeral port, release it, and confirm it reads as available.
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    assert cli._port_available("127.0.0.1", port) is True


def test_port_available_false_when_taken():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    port = s.getsockname()[1]
    try:
        assert cli._port_available("127.0.0.1", port) is False
    finally:
        s.close()


def test_main_exits_when_port_taken(monkeypatch):
    monkeypatch.setattr(cli, "_port_available", lambda h, p: False)
    monkeypatch.setattr(cli.uvicorn, "run", lambda *a, **k: pytest.fail("should not start"))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1


# --- from tests/test_health.py -----------------------------------------------


@pytest.mark.anyio
async def test_healthz(client):
    r = await client.get("/healthz")
    # status_code depends on whether `claude` is on PATH (200 healthy / 503
    # degraded) — assert on the checks structure instead of the composite
    # status so this passes in CI without the CLI installed.
    assert r.status_code in (200, 503)
    body = r.json()
    assert body["checks"]["db"] is True
    assert "claude_cli" in body["checks"]


# --- from tests/test_health_checks.py ----------------------------------------


@pytest.mark.anyio
async def test_healthz_ok_when_claude_cli_present(client, monkeypatch):
    monkeypatch.setattr("app.main.shutil.which", lambda name: "/usr/local/bin/claude")

    r = await client.get("/healthz")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["checks"]["claude_cli"] is True
    assert body["checks"]["db"] is True


@pytest.mark.anyio
async def test_healthz_degraded_when_claude_cli_missing(client, monkeypatch):
    monkeypatch.setattr("app.main.shutil.which", lambda name: None)

    r = await client.get("/healthz")

    assert r.status_code == 503
    body = r.json()
    assert body["status"] != "ok"
    assert body["checks"]["claude_cli"] is False
