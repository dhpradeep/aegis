from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from app.main import create_app

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    from app.services import login_throttle

    login_throttle.reset()
    yield
    login_throttle.reset()

@pytest.fixture
async def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    # Build the ephemeral test schema from the models (create_all) rather than
    # running Alembic migrations per test.
    monkeypatch.setenv("RUN_MIGRATIONS_ON_STARTUP", "false")
    # No live model fetch in tests: use the curated fallback (no network/keychain).
    monkeypatch.setenv("MODELS_LIVE_FETCH", "false")

    from app.core.config import get_settings
    get_settings.cache_clear()
    import app.db.base as base
    base.reset_engine()
    # The model catalog caches across the process; clear it so a prior test's
    # fallback entry doesn't leak into a test that configures live fetch.
    import app.services.models as models_module
    models_module._CACHE["models"] = None
    models_module._CACHE["expires"] = 0.0

    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # exposes the ASGI app handle, e.g. to set app.state.runtime
            yield c

    # Close pooled aiosqlite connections while this test's loop is still
    # alive; otherwise their worker threads fire "Event loop is closed".
    await base.engine.dispose()
    get_settings.cache_clear()


@dataclass
class AuthedClient:
    """Bundles an authed httpx client with the tenant/key it was seeded for."""

    client: AsyncClient
    headers: dict
    tenant_id: str
    key_id: str
    app: object  # the FastAPI app handle (client.app); e.g. authed_client.app.state.runtime = ...


async def _seed_key(tenant_id: str, key_id: str, *, is_admin: bool = False) -> str:
    """Seed a tenant (if new) + API key, returning the full bearer token."""
    from app.core.security import generate_api_key
    from app.db.base import SessionLocal
    from app.db.models import ApiKey, Tenant

    full_key, prefix, key_hash = generate_api_key()
    async with SessionLocal() as db:
        existing = await db.get(Tenant, tenant_id)
        if existing is None:
            db.add(Tenant(id=tenant_id, name=tenant_id))
        db.add(
            ApiKey(
                id=key_id,
                tenant_id=tenant_id,
                key_hash=key_hash,
                prefix=prefix,
                name=key_id,
                rpm=30,
                daily_cost_usd=10.0,
                is_admin=is_admin,
            )
        )
        await db.commit()
    return full_key


async def _default_agent_id() -> str:
    """Return the id of the built-in `default` agent (seeded on app startup)."""
    from sqlalchemy import select
    from app.db.base import SessionLocal
    from app.db.models import Agent

    async with SessionLocal() as db:
        row = (
            await db.execute(select(Agent).where(Agent.name == "default"))
        ).scalar_one()
        return row.id


async def _seed_agent(name: str, *, is_admin_only: bool = False, allowed_tools=None) -> str:
    """Create an Agent and return its id."""
    from app.db.base import SessionLocal
    from app.services.agents import create_agent

    async with SessionLocal() as db:
        agent = await create_agent(
            db,
            name=name,
            model="claude-sonnet-5",
            allowed_tools=allowed_tools if allowed_tools is not None else ["Read", "Write"],
            is_admin_only=is_admin_only,
        )
        return agent.id


@pytest.fixture
async def authed_client(client) -> AuthedClient:
    """An httpx client plus a seeded non-admin API key for a fresh tenant."""
    full_key = await _seed_key("t_test", "k_test", is_admin=False)
    return AuthedClient(
        client=client,
        headers={"Authorization": f"Bearer {full_key}"},
        tenant_id="t_test",
        key_id="k_test",
        app=client.app,
    )
