import json
from secrets import token_hex

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.models import McpServer

TENANT_ALLOWED_KINDS = {"http", "sse"}
# Remote kinds admins may configure from the dashboard. `stdio` is intentionally
# excluded from the UI — it runs a local command and is better managed directly.
ADMIN_ALLOWED_KINDS = {"http", "sse"}


def tenant_kind_allowed(kind: str) -> bool:
    """Whether `kind` may be used by a tenant-owned MCP server row."""
    return kind in TENANT_ALLOWED_KINDS


async def list_mcp_servers(db: AsyncSession) -> list[McpServer]:
    """All MCP servers (global + every tenant), ordered by name."""
    return list(
        (await db.execute(select(McpServer).order_by(McpServer.name))).scalars().all()
    )


async def create_mcp_server(
    db: AsyncSession,
    *,
    name: str,
    kind: str,
    url: str,
    headers: dict | None = None,
    tenant_id: str | None = None,
) -> McpServer:
    """Create an `http`/`sse` MCP server, global (`tenant_id=None`) or
    tenant-scoped. Names must be unique within their scope."""
    name = name.strip()
    if not name:
        raise ApiError.invalid("name is required")
    if kind not in ADMIN_ALLOWED_KINDS:
        raise ApiError.invalid(f"Unsupported MCP server type: {kind}")
    if not url.strip():
        raise ApiError.invalid("url is required")

    existing = (
        await db.execute(
            select(McpServer).where(
                McpServer.tenant_id.is_(tenant_id) if tenant_id is None
                else McpServer.tenant_id == tenant_id,
                McpServer.name == name,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ApiError.invalid("an MCP server with that name already exists in this scope")

    row = McpServer(
        id="mcp_" + token_hex(8),
        tenant_id=tenant_id,
        name=name,
        kind=kind,
        url=url.strip(),
        headers_json=json.dumps(headers) if headers else None,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def delete_mcp_server(db: AsyncSession, mcp_id: str) -> None:
    """Delete an MCP server by id. Raises not_found if absent."""
    row = await db.get(McpServer, mcp_id)
    if row is None:
        raise ApiError.not_found("MCP server not found")
    await db.delete(row)
    await db.commit()


def _build_config(row: McpServer) -> dict:
    if row.kind in ("http", "sse"):
        config: dict = {"type": row.kind, "url": row.url}
        if row.headers_json is not None:
            config["headers"] = json.loads(row.headers_json)
        return config

    if row.kind == "stdio":
        if row.tenant_id is not None:
            raise ApiError.invalid(
                f"MCP server '{row.name}' is a tenant-owned stdio server, which is not allowed"
            )
        return {
            "type": "stdio",
            "command": row.command,
            "args": json.loads(row.args_json or "[]"),
            "env": {},
        }

    raise ApiError.invalid(f"MCP server '{row.name}' has unknown kind '{row.kind}'")


async def resolve_mcp(
    db: AsyncSession, tenant_id: str, names: list[str], extra_names: list[str] | None = None
) -> dict[str, dict]:
    """Resolve MCP server names into claude-agent-sdk mcp_servers config.

    Each name in the union of `names` and `extra_names` is looked up among
    McpServer rows that are either global (tenant_id IS NULL) or owned by
    `tenant_id`. Unknown names raise ApiError.invalid.
    """
    all_names = list(dict.fromkeys([*names, *(extra_names or [])]))
    if not all_names:
        return {}

    result = await db.execute(
        select(McpServer).where(
            McpServer.name.in_(all_names),
            (McpServer.tenant_id.is_(None)) | (McpServer.tenant_id == tenant_id),
        )
    )
    rows = {row.name: row for row in result.scalars().all()}

    resolved: dict[str, dict] = {}
    for name in all_names:
        row = rows.get(name)
        if row is None:
            raise ApiError.invalid(f"Unknown MCP server: {name}")
        resolved[name] = _build_config(row)

    return resolved
