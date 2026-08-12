import asyncio
import shutil
import time
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ApiError


def workspace_path(tenant_id: str, session_id: str) -> Path:
    """Return the on-disk path for a tenant/session workspace (does not create it)."""
    settings = get_settings()
    return Path(settings.workspace_root) / tenant_id / session_id


def create_workspace(tenant_id: str, session_id: str) -> Path:
    """Create (if needed) and return the workspace directory for a tenant/session."""
    ws = workspace_path(tenant_id, session_id)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def resolve_in_workspace(ws: Path, rel: str) -> Path:
    """Resolve `rel` against `ws`, raising ApiError if the result escapes `ws`.

    Note: if `rel` is itself an absolute path, pathlib's `/` operator discards
    `ws` entirely (standard pathlib semantics), so the resolved path will not
    be relative to `ws` and this correctly raises rather than silently
    reinterpreting it as workspace-relative.
    """
    ws_resolved = ws.resolve()
    resolved = (ws / rel).resolve()
    if not resolved.is_relative_to(ws_resolved):
        raise ApiError.invalid("path escapes workspace")
    return resolved


def workspace_size_mb(ws: Path) -> float:
    """Return the total size in MB of all files under `ws`."""
    total_bytes = sum(f.stat().st_size for f in ws.rglob("*") if f.is_file())
    return total_bytes / (1024 * 1024)


def enforce_quota(ws: Path, limit_mb: int) -> None:
    """Raise ApiError if the workspace's total size exceeds `limit_mb`."""
    if workspace_size_mb(ws) > limit_mb:
        raise ApiError.invalid("workspace quota exceeded")


def list_files(ws: Path) -> list[dict]:
    """List all files under `ws` as {"path": <relative str>, "size": <int>} dicts."""
    files = []
    for f in sorted(ws.rglob("*")):
        if f.is_file():
            files.append({"path": str(f.relative_to(ws)), "size": f.stat().st_size})
    return files


async def cleanup_expired(root: Path, ttl_days: int) -> None:
    """Remove session workspace directories under `root` whose mtime is older than ttl_days."""

    def _cleanup() -> None:
        cutoff = time.time() - (ttl_days * 86400)
        root_path = Path(root)
        if not root_path.is_dir():
            return
        for tenant_dir in root_path.iterdir():
            if not tenant_dir.is_dir():
                continue
            for session_dir in tenant_dir.iterdir():
                if session_dir.is_dir() and session_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(session_dir, ignore_errors=True)

    await asyncio.to_thread(_cleanup)
