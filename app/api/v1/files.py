from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.api.v1.sessions import owned_session
from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.models import ApiKey
from app.services.workspaces import enforce_quota, list_files, resolve_in_workspace

router = APIRouter(prefix="/v1/sessions/{session_id}/files", tags=["files"])


@router.post("")
async def upload_file(
    session_id: str,
    file: UploadFile,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await owned_session(db, key.tenant_id, session_id)
    ws = Path(session.workspace_path)

    if not file.filename:
        raise ApiError.invalid("filename required")

    dest = resolve_in_workspace(ws, file.filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    contents = await file.read()
    dest.write_bytes(contents)

    enforce_quota(ws, get_settings().workspace_quota_mb)

    return {"path": str(dest.relative_to(ws)), "size": dest.stat().st_size}


@router.get("")
async def get_files(
    session_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    session = await owned_session(db, key.tenant_id, session_id)
    return list_files(Path(session.workspace_path))


@router.get("/{path:path}")
async def download_file(
    session_id: str,
    path: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    session = await owned_session(db, key.tenant_id, session_id)
    ws = Path(session.workspace_path)

    target = resolve_in_workspace(ws, path)
    if not target.is_file():
        raise ApiError.not_found("File not found")

    return FileResponse(target)
