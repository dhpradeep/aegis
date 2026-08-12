import os
import time

import pytest

from app.core.errors import ApiError
from app.services.workspaces import (
    cleanup_expired,
    create_workspace,
    enforce_quota,
    list_files,
    resolve_in_workspace,
    workspace_path,
    workspace_size_mb,
)


def test_workspace_path_is_root_tenant_session(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    from app.core.config import get_settings
    get_settings.cache_clear()

    ws = workspace_path("t1", "s1")

    assert ws == tmp_path / "t1" / "s1"


def test_create_workspace_makes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path))
    from app.core.config import get_settings
    get_settings.cache_clear()

    ws = create_workspace("t1", "s1")

    assert ws.is_dir()
    assert ws == tmp_path / "t1" / "s1"


def test_resolve_in_workspace_rejects_traversal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    with pytest.raises(ApiError) as exc_info:
        resolve_in_workspace(ws, "../../etc/passwd")

    assert exc_info.value.status == 422
    assert "escapes" in exc_info.value.message


def test_resolve_in_workspace_rejects_absolute_traversal(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    with pytest.raises(ApiError):
        resolve_in_workspace(ws, "/etc/passwd")


def test_resolve_in_workspace_allows_nested_path(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    resolved = resolve_in_workspace(ws, "out/a.txt")

    assert resolved == (ws / "out" / "a.txt").resolve()
    assert resolved.is_relative_to(ws.resolve())


def test_list_files_returns_relative_path_and_size(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "a.txt").write_text("hello")
    (ws / "sub" / "b.txt").write_text("hi there")

    files = list_files(ws)
    by_path = {f["path"]: f["size"] for f in files}

    assert by_path == {
        "a.txt": len("hello"),
        os.path.join("sub", "b.txt"): len("hi there"),
    }


def test_list_files_empty_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()

    assert list_files(ws) == []


def test_workspace_size_mb_sums_file_sizes(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.txt").write_bytes(b"x" * (1024 * 1024))
    (ws / "b.txt").write_bytes(b"x" * (1024 * 1024))

    assert workspace_size_mb(ws) == pytest.approx(2.0, rel=1e-3)


def test_enforce_quota_raises_when_over_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_bytes(b"x" * (2 * 1024 * 1024))

    with pytest.raises(ApiError) as exc_info:
        enforce_quota(ws, limit_mb=1)

    assert exc_info.value.status == 422
    assert "quota" in exc_info.value.message


def test_enforce_quota_passes_when_under_limit(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "small.txt").write_bytes(b"x" * 1024)

    enforce_quota(ws, limit_mb=1)  # should not raise


@pytest.mark.anyio
async def test_cleanup_expired_removes_old_workspaces(tmp_path):
    root = tmp_path / "root"
    old_ws = root / "t1" / "old-session"
    new_ws = root / "t1" / "new-session"
    old_ws.mkdir(parents=True)
    new_ws.mkdir(parents=True)

    old_time = time.time() - (10 * 86400)
    os.utime(old_ws, (old_time, old_time))

    await cleanup_expired(root, ttl_days=7)

    assert not old_ws.exists()
    assert new_ws.exists()


@pytest.mark.anyio
async def test_lifespan_runs_startup_sweep_and_cancels_cleanup_task_cleanly(
    tmp_path, monkeypatch
):
    """Regression test for wiring cleanup_expired into the app lifespan.

    `cleanup_expired` was implemented and unit-tested but never scheduled
    anywhere. This asserts the lifespan (a) performs a sweep that removes an
    already-aged workspace directory at startup, and (b) cancels its
    background cleanup task cleanly on shutdown (no exception escapes).
    """
    ws_root = tmp_path / "ws"
    old_ws = ws_root / "t1" / "old-session"
    new_ws = ws_root / "t1" / "new-session"
    old_ws.mkdir(parents=True)
    new_ws.mkdir(parents=True)

    old_time = time.time() - (30 * 86400)
    os.utime(old_ws, (old_time, old_time))

    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(ws_root))
    monkeypatch.setenv("WORKSPACE_TTL_DAYS", "7")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("SESSION_SECRET", "test-secret")

    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.db.base as base

    base.reset_engine()

    from app.main import create_app

    app = create_app()

    async with app.router.lifespan_context(app):
        # The startup sweep runs (and is awaited) before lifespan yields.
        assert not old_ws.exists()
        assert new_ws.exists()

        cleanup_task = app.state.cleanup_task
        assert cleanup_task is not None
        assert not cleanup_task.done()

    # Shutdown cancelled the task cleanly (no exception raised out of the
    # lifespan context above), and the task itself ended up cancelled.
    assert cleanup_task.cancelled()

    get_settings.cache_clear()
