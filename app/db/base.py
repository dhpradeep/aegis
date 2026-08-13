from pathlib import Path
from typing import Optional

from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


def ensure_sqlite_dir(database_url: str) -> None:
    """Create the parent directory for a file-backed SQLite database, since
    SQLite won't create it and a fresh checkout has no `data/` dir yet."""
    url = make_url(database_url)
    if not url.get_backend_name().startswith("sqlite"):
        return
    db_path = url.database
    if not db_path or db_path == ":memory:":
        return
    Path(db_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


class _EngineProxy:
    """Stable handle whose underlying engine can be swapped by reset_engine().

    Code that does `from app.db.base import engine` binds this proxy object
    once. reset_engine() then mutates the proxy's target in place, so every
    holder of the proxy (old imports included) observes the new engine
    instead of being stuck with a stale reference.
    """

    _target: Optional[AsyncEngine] = None

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __repr__(self):
        return f"<EngineProxy -> {self._target!r}>"


class _SessionLocalProxy:
    """Stable callable whose underlying sessionmaker can be swapped by reset_engine()."""

    _target: Optional[async_sessionmaker] = None

    def __call__(self, *args, **kwargs) -> AsyncSession:
        return self._target(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __repr__(self):
        return f"<SessionLocalProxy -> {self._target!r}>"


engine = _EngineProxy()
SessionLocal = _SessionLocalProxy()


def reset_engine() -> None:
    """(Re)build the async engine and session factory from current settings.

    Rebuilds engine/SessionLocal in place (via the proxies above) rather than
    rebinding module-level names, so callers that already did
    `from app.db.base import engine, SessionLocal` keep working correctly
    after DATABASE_URL changes and this is called again (see tests).
    """
    global engine, SessionLocal
    settings = get_settings()
    ensure_sqlite_dir(settings.database_url)
    new_engine = create_async_engine(settings.database_url, future=True, pool_pre_ping=True)
    engine._target = new_engine
    SessionLocal._target = async_sessionmaker(
        new_engine, expire_on_commit=False, class_=AsyncSession
    )


reset_engine()


async def init_db() -> None:
    """Build the schema directly from the models (create_all).

    Used by the **test suite** (fast, ephemeral per-test DBs) and as the dev
    bootstrap. The **persistent/production** schema is managed by Alembic —
    run `alembic upgrade head` (the app lifespan does this automatically for a
    real DB; see app/db/migrate.py). create_all is intentionally not used to
    evolve an existing persistent DB, since it never alters existing tables.
    """
    import app.db.models  # noqa: F401  register mappers

    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    async with SessionLocal() as db:
        yield db
