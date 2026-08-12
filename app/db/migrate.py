"""Programmatic Alembic migration runner.

The app lifespan calls `run_migrations()` on startup (for a persistent DB) to
bring the schema to head. Alembic is synchronous, so it runs in a worker
thread; the migration environment (migrations/env.py) reads the same
`DATABASE_URL` the app uses and runs against a sync SQLite driver.
"""

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> Config:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    return cfg


def _upgrade_head() -> None:
    command.upgrade(_alembic_config(), "head")


async def run_migrations() -> None:
    """Upgrade the database to the latest migration (`alembic upgrade head`)."""
    from app.core.config import get_settings
    from app.db.base import ensure_sqlite_dir

    ensure_sqlite_dir(get_settings().database_url)
    await asyncio.to_thread(_upgrade_head)
