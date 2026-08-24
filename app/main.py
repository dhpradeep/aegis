import asyncio
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text
from app.core.config import get_settings
from app.core.errors import install_error_handlers
from app.core.logging import setup_logging, RequestIDMiddleware

logger = logging.getLogger("app.workspaces")

# Bounded wait (seconds) for in-flight agent runs to drain on shutdown.
SHUTDOWN_DRAIN_TIMEOUT_S = 5.0

# Interval between periodic workspace TTL cleanup sweeps.
WORKSPACE_CLEANUP_INTERVAL_S = 3600.0


def _enclosing_git_repo(path: Path) -> Path | None:
    """Return the nearest ancestor (or self) containing a `.git`, else None."""
    for p in [path, *path.parents]:
        if (p / ".git").exists():
            return p
    return None


async def _workspace_cleanup_loop(root: Path, ttl_days: int) -> None:
    """Periodically sweep expired workspaces. Runs until cancelled.

    Each sweep is wrapped in try/except so a single failed sweep (e.g. a
    transient filesystem error) doesn't kill the loop or the app.
    """
    from app.services.workspaces import cleanup_expired

    while True:
        await asyncio.sleep(WORKSPACE_CLEANUP_INTERVAL_S)
        try:
            await cleanup_expired(root, ttl_days)
        except Exception:
            logger.exception("workspace cleanup sweep failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    from app.db.base import init_db, SessionLocal
    from app.services.admin import bootstrap_admin_if_needed
    from app.services.agent.runtime import AgentRuntime
    from app.services.ratelimit import init_run_gate
    from app.services.workspaces import cleanup_expired

    # Persistent DBs are schema-managed by Alembic (run `alembic upgrade head`);
    # the test suite sets run_migrations_on_startup=False and builds the schema
    # from the models via create_all instead.
    if get_settings().run_migrations_on_startup:
        from app.db.migrate import run_migrations

        await run_migrations()
    else:
        await init_db()
    init_run_gate(get_settings().max_concurrent_runs)
    from app.services.agents import seed_default_agent
    async with SessionLocal() as _db:
        await seed_default_agent(_db)
    app.state.runtime = AgentRuntime()

    if os.environ.get("BOOTSTRAP_ADMIN") == "1":
        async with SessionLocal() as db:
            await bootstrap_admin_if_needed(db)

    # Run one workspace-TTL sweep immediately at startup, then schedule a
    # background task to repeat it every WORKSPACE_CLEANUP_INTERVAL_S.
    settings = get_settings()
    workspace_root = Path(settings.workspace_root)
    repo = _enclosing_git_repo(workspace_root)
    if repo is not None:
        logging.getLogger("app").warning(
            "WORKSPACE_ROOT %s is inside a git repository (%s). Agent runs may "
            "inherit that project's CLAUDE.md/memory context. Set WORKSPACE_ROOT "
            "to a path outside any git repo.",
            workspace_root,
            repo,
        )
    try:
        await cleanup_expired(workspace_root, settings.workspace_ttl_days)
    except Exception:
        logger.exception("startup workspace cleanup sweep failed")

    cleanup_task = asyncio.create_task(
        _workspace_cleanup_loop(workspace_root, settings.workspace_ttl_days)
    )
    app.state.cleanup_task = cleanup_task

    yield

    # Graceful shutdown: wait (bounded) for any in-flight agent runs to
    # release the run_gate before the app finishes shutting down.
    from app.services.ratelimit import run_gate
    if run_gate is not None:
        deadline = time.monotonic() + SHUTDOWN_DRAIN_TIMEOUT_S
        while run_gate.in_flight() > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    # Cancel the background workspace cleanup task cleanly.
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

def create_app() -> FastAPI:
    app = FastAPI(title="Aegis", lifespan=lifespan)
    app.add_middleware(RequestIDMiddleware)
    install_error_handlers(app)

    from fastapi.staticfiles import StaticFiles

    _static_dir = Path(__file__).parent / "api" / "admin" / "static"
    app.mount(
        "/admin/static",
        StaticFiles(directory=str(_static_dir)),
        name="admin-static",
    )

    from app.api.admin.api import router as admin_router
    from app.api.admin.ui import install_admin_ui_handlers
    from app.api.admin.ui import router as admin_ui_router
    from app.api.compat.anthropic import router as anthropic_compat_router
    from app.api.compat.openai import router as openai_compat_router
    from app.api.v1.router import v1_router
    app.include_router(v1_router)
    app.include_router(openai_compat_router)
    app.include_router(anthropic_compat_router)
    app.include_router(admin_router)
    # The admin dashboard is server-rendered HTML, not a JSON API — keep it out
    # of the OpenAPI schema / Swagger docs.
    app.include_router(admin_ui_router, include_in_schema=False)
    install_admin_ui_handlers(app)

    @app.get("/healthz")
    async def healthz():
        from app.db.base import SessionLocal

        checks = {"db": False, "claude_cli": False}

        try:
            async with SessionLocal() as db:
                await db.execute(text("SELECT 1"))
            checks["db"] = True
        except Exception:
            checks["db"] = False

        checks["claude_cli"] = shutil.which("claude") is not None

        status = "ok" if all(checks.values()) else "degraded"
        payload = {"status": status, "checks": checks}
        if status != "ok":
            return JSONResponse(status_code=503, content=payload)
        return payload

    return app
