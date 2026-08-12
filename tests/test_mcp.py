import json

import pytest

from app.core.errors import ApiError
from app.db.base import SessionLocal, init_db, reset_engine
from app.db.models import McpServer, Tenant
from app.services.mcp import (
    create_mcp_server,
    delete_mcp_server,
    list_mcp_servers,
    resolve_mcp,
    tenant_kind_allowed,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'mcp.db'}")
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.db.base as base

    base.reset_engine()
    await init_db()

    async with SessionLocal() as session:
        session.add(Tenant(id="t1", name="Acme"))
        session.add(
            McpServer(
                id="m1",
                tenant_id=None,
                name="web",
                kind="http",
                url="https://example.com/mcp",
                headers_json=None,
                command=None,
                args_json=None,
            )
        )
        session.add(
            McpServer(
                id="m2",
                tenant_id="t1",
                name="notes",
                kind="sse",
                url="https://notes.example.com/mcp",
                headers_json=json.dumps({"Authorization": "Bearer xyz"}),
                command=None,
                args_json=None,
            )
        )
        session.add(
            McpServer(
                id="m3",
                tenant_id=None,
                name="local-tool",
                kind="stdio",
                url=None,
                headers_json=None,
                command="my-tool",
                args_json=json.dumps(["--flag"]),
            )
        )
        await session.commit()

    async with SessionLocal() as session:
        yield session


@pytest.fixture
async def admin_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'mcp_admin.db'}")
    from app.core.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    await init_db()
    async with SessionLocal() as session:
        yield session


@pytest.mark.anyio
async def test_resolve_mcp_global_and_tenant(db):
    result = await resolve_mcp(db, "t1", ["web", "notes"], [])

    assert result["web"] == {"type": "http", "url": "https://example.com/mcp"}
    assert result["notes"] == {
        "type": "sse",
        "url": "https://notes.example.com/mcp",
        "headers": {"Authorization": "Bearer xyz"},
    }


@pytest.mark.anyio
async def test_resolve_mcp_stdio_global_allowed(db):
    result = await resolve_mcp(db, "t1", ["local-tool"], [])

    assert result["local-tool"] == {
        "type": "stdio",
        "command": "my-tool",
        "args": ["--flag"],
        "env": {},
    }


@pytest.mark.anyio
async def test_resolve_mcp_merges_extra_names(db):
    result = await resolve_mcp(db, "t1", ["web"], ["notes"])

    assert set(result.keys()) == {"web", "notes"}


@pytest.mark.anyio
async def test_resolve_mcp_unknown_name_raises(db):
    with pytest.raises(ApiError) as exc_info:
        await resolve_mcp(db, "t1", ["ghost"], [])

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_resolve_mcp_tenant_cannot_see_other_tenant_server(db):
    with pytest.raises(ApiError):
        await resolve_mcp(db, "t2", ["notes"], [])


@pytest.mark.anyio
async def test_resolve_mcp_no_headers_omits_key(db):
    result = await resolve_mcp(db, "t1", ["web"], [])

    assert "headers" not in result["web"]


def test_tenant_kind_allowed():
    assert tenant_kind_allowed("http") is True
    assert tenant_kind_allowed("sse") is True
    assert tenant_kind_allowed("stdio") is False
    assert tenant_kind_allowed("bogus") is False


@pytest.mark.anyio
async def test_create_mcp_server_disallowed_kind_returns_422(authed_client):
    r = await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "n", "type": "stdio", "url": "x"},
        headers=authed_client.headers,
    )

    assert r.status_code == 422


@pytest.mark.anyio
async def test_create_mcp_server_valid_http(authed_client):
    r = await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "web", "type": "http", "url": "https://example.com/mcp"},
        headers=authed_client.headers,
    )

    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("mcp_")
    assert body["name"] == "web"
    assert body["type"] == "http"
    assert body["url"] == "https://example.com/mcp"


@pytest.mark.anyio
async def test_create_mcp_server_duplicate_name_returns_422(authed_client):
    await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "web", "type": "http", "url": "https://example.com/mcp"},
        headers=authed_client.headers,
    )

    r = await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "web", "type": "sse", "url": "https://example.com/mcp2"},
        headers=authed_client.headers,
    )

    assert r.status_code == 422


@pytest.mark.anyio
async def test_list_mcp_servers_includes_created_server(authed_client):
    await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "web", "type": "http", "url": "https://example.com/mcp"},
        headers=authed_client.headers,
    )

    r = await authed_client.client.get("/v1/mcp-servers", headers=authed_client.headers)

    assert r.status_code == 200
    names = {s["name"]: s for s in r.json()}
    assert "web" in names
    assert names["web"]["read_only"] is False


@pytest.mark.anyio
async def test_list_mcp_servers_includes_global_as_read_only(authed_client):
    async with SessionLocal() as db:
        db.add(
            McpServer(
                id="mcp_global1",
                tenant_id=None,
                name="global-web",
                kind="http",
                url="https://global.example.com/mcp",
            )
        )
        await db.commit()

    r = await authed_client.client.get("/v1/mcp-servers", headers=authed_client.headers)

    assert r.status_code == 200
    names = {s["name"]: s for s in r.json()}
    assert names["global-web"]["read_only"] is True


@pytest.mark.anyio
async def test_delete_mcp_server_removes_tenant_owned(authed_client):
    await authed_client.client.post(
        "/v1/mcp-servers",
        json={"name": "web", "type": "http", "url": "https://example.com/mcp"},
        headers=authed_client.headers,
    )

    r = await authed_client.client.delete(
        "/v1/mcp-servers/web", headers=authed_client.headers
    )
    assert r.status_code == 200

    listing = await authed_client.client.get(
        "/v1/mcp-servers", headers=authed_client.headers
    )
    assert "web" not in {s["name"] for s in listing.json()}


@pytest.mark.anyio
async def test_delete_mcp_server_absent_returns_404(authed_client):
    r = await authed_client.client.delete(
        "/v1/mcp-servers/does-not-exist", headers=authed_client.headers
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_delete_mcp_server_global_returns_404(authed_client):
    async with SessionLocal() as db:
        db.add(
            McpServer(
                id="mcp_global2",
                tenant_id=None,
                name="global-web2",
                kind="http",
                url="https://global.example.com/mcp",
            )
        )
        await db.commit()

    r = await authed_client.client.delete(
        "/v1/mcp-servers/global-web2", headers=authed_client.headers
    )

    assert r.status_code == 404


@pytest.mark.anyio
async def test_create_global_mcp_server_and_resolve(admin_db):
    await create_mcp_server(
        admin_db, name="docs", kind="http", url="https://mcp.example.com", tenant_id=None
    )

    servers = await list_mcp_servers(admin_db)
    assert [s.name for s in servers] == ["docs"]
    assert servers[0].tenant_id is None

    # A global server resolves for any tenant.
    resolved = await resolve_mcp(admin_db, "any_tenant", ["docs"])
    assert resolved["docs"]["type"] == "http"
    assert resolved["docs"]["url"] == "https://mcp.example.com"


@pytest.mark.anyio
async def test_create_with_headers(admin_db):
    row = await create_mcp_server(
        admin_db,
        name="authed",
        kind="sse",
        url="https://mcp.example.com/sse",
        headers={"Authorization": "Bearer tok"},
    )
    assert row.headers_json is not None
    resolved = await resolve_mcp(admin_db, "t1", ["authed"])
    assert resolved["authed"]["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.anyio
async def test_unsupported_kind_rejected(admin_db):
    with pytest.raises(ApiError):
        await create_mcp_server(admin_db, name="x", kind="stdio", url="", tenant_id=None)


@pytest.mark.anyio
async def test_duplicate_name_in_same_scope_rejected(admin_db):
    await create_mcp_server(admin_db, name="dup", kind="http", url="https://a", tenant_id=None)
    with pytest.raises(ApiError):
        await create_mcp_server(admin_db, name="dup", kind="http", url="https://b", tenant_id=None)


@pytest.mark.anyio
async def test_missing_url_rejected(admin_db):
    with pytest.raises(ApiError):
        await create_mcp_server(admin_db, name="nourl", kind="http", url="  ", tenant_id=None)


@pytest.mark.anyio
async def test_delete_mcp_server(admin_db):
    row = await create_mcp_server(admin_db, name="temp", kind="http", url="https://a")
    await delete_mcp_server(admin_db, row.id)
    assert await list_mcp_servers(admin_db) == []


@pytest.mark.anyio
async def test_delete_missing_raises(admin_db):
    with pytest.raises(ApiError):
        await delete_mcp_server(admin_db, "mcp_ghost")
