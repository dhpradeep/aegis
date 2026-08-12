import json
from secrets import token_hex

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey, McpServer
from app.schemas.mcp import McpServerCreateRequest, McpServerResponse
from app.services.mcp import tenant_kind_allowed

router = APIRouter(prefix="/v1/mcp-servers", tags=["mcp-servers"])

__all__ = ["router"]


@router.post("", response_model=McpServerResponse)
async def create_mcp_server(
    body: McpServerCreateRequest,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> McpServerResponse:
    if not tenant_kind_allowed(body.type):
        raise ApiError.invalid(f"Unsupported MCP server type: {body.type}")

    existing = (
        await db.execute(
            select(McpServer).where(
                McpServer.tenant_id == key.tenant_id, McpServer.name == body.name
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError.invalid("name exists")

    row = McpServer(
        id="mcp_" + token_hex(8),
        tenant_id=key.tenant_id,
        name=body.name,
        kind=body.type,
        url=body.url,
        headers_json=json.dumps(body.headers) if body.headers else None,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ApiError.invalid("name exists")
    await db.refresh(row)

    return McpServerResponse(id=row.id, name=row.name, type=row.kind, url=row.url)


@router.get("", response_model=list[McpServerResponse])
async def list_mcp_servers(
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> list[McpServerResponse]:
    rows = (
        (
            await db.execute(
                select(McpServer).where(
                    (McpServer.tenant_id == key.tenant_id)
                    | (McpServer.tenant_id.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        McpServerResponse(
            id=row.id,
            name=row.name,
            type=row.kind,
            url=row.url,
            read_only=row.tenant_id is None,
        )
        for row in rows
    ]


@router.delete("/{name}")
async def delete_mcp_server(
    name: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    row = (
        await db.execute(
            select(McpServer).where(
                McpServer.tenant_id == key.tenant_id, McpServer.name == name
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError.not_found("MCP server not found")

    await db.delete(row)
    await db.commit()
    return {"status": "deleted"}
