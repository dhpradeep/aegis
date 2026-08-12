import json
import re

import pytest
from sqlalchemy import select

from claude_agent_sdk import AgentDefinition

from app.core.errors import ApiError
from app.db.base import SessionLocal, init_db, reset_engine
from app.db.models import Agent, Session
from app.services.agent.runtime import AgentRuntime, RunConfig
from app.services.agent.session_runner import build_run_config
from app.services.agent.subagents import roster_to_agent_defs
from app.services.agents import (
    create_agent,
    delete_agent,
    get_agent,
    list_agents,
    seed_default_agent,
    update_agent,
)
from tests.conftest import _seed_key


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# From tests/test_agents_service.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'agents.db'}")
    from app.core.config import get_settings

    get_settings.cache_clear()
    import app.db.base as base

    base.reset_engine()
    await init_db()

    async with SessionLocal() as session:
        yield session


async def _leaf_agent(db, name: str) -> Agent:
    """Create a simple agent with no roster of its own."""
    return await create_agent(
        db,
        name=name,
        model="claude-sonnet-5",
        allowed_tools=["Read"],
    )


@pytest.mark.anyio
async def test_seed_default_agent_creates_only_default(db):
    await seed_default_agent(db)

    agents = await list_agents(db)
    names = {a.name for a in agents}
    assert names == {"default"}

    default = next(a for a in agents if a.name == "default")
    assert default.is_admin_only is False
    assert default.bypass_permissions is False
    assert json.loads(default.roster_json) == []
    assert "Bash" not in json.loads(default.allowed_tools_json)


@pytest.mark.anyio
async def test_seed_default_agent_is_idempotent(db):
    await seed_default_agent(db)
    await seed_default_agent(db)

    agents = await list_agents(db)
    names = [a.name for a in agents]
    assert names.count("default") == 1


@pytest.mark.anyio
async def test_create_agent_success(db):
    agent = await create_agent(
        db,
        name="researcher",
        model="claude-sonnet-5",
        description="Does research",
        allowed_tools=["Read", "WebSearch"],
    )

    assert agent.id.startswith("agt_")
    assert agent.name == "researcher"
    assert json.loads(agent.allowed_tools_json) == ["Read", "WebSearch"]
    assert agent.roster_json == "[]"
    assert agent.permission_mode == "default"
    assert agent.max_iterations == 6

    fetched = await get_agent(db, agent.id)
    assert fetched is not None
    assert fetched.name == "researcher"


@pytest.mark.anyio
async def test_create_agent_duplicate_name_raises(db):
    await create_agent(db, name="dup", model="claude-sonnet-5", allowed_tools=[])

    with pytest.raises(ApiError) as exc_info:
        await create_agent(db, name="dup", model="claude-sonnet-5", allowed_tools=[])

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_create_agent_roster_nonexistent_id_raises(db):
    with pytest.raises(ApiError) as exc_info:
        await create_agent(
            db,
            name="manager",
            model="claude-sonnet-5",
            allowed_tools=[],
            roster=["agt_doesnotexist"],
        )

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_create_agent_roster_max_5_raises(db):
    leaf_ids = [(await _leaf_agent(db, f"leaf{i}")).id for i in range(6)]

    with pytest.raises(ApiError) as exc_info:
        await create_agent(
            db,
            name="manager",
            model="claude-sonnet-5",
            allowed_tools=[],
            roster=leaf_ids,
        )

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_create_agent_roster_member_with_own_roster_raises(db):
    leaf = await _leaf_agent(db, "leaf")
    manager = await create_agent(
        db,
        name="manager",
        model="claude-sonnet-5",
        allowed_tools=[],
        roster=[leaf.id],
    )

    with pytest.raises(ApiError) as exc_info:
        await create_agent(
            db,
            name="director",
            model="claude-sonnet-5",
            allowed_tools=[],
            roster=[manager.id],
        )

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_create_agent_valid_roster_of_two_leaf_agents_succeeds(db):
    leaf_a = await _leaf_agent(db, "leaf_a")
    leaf_b = await _leaf_agent(db, "leaf_b")

    manager = await create_agent(
        db,
        name="manager",
        model="claude-sonnet-5",
        allowed_tools=[],
        roster=[leaf_a.id, leaf_b.id],
    )

    assert set(json.loads(manager.roster_json)) == {leaf_a.id, leaf_b.id}


@pytest.mark.anyio
async def test_update_agent_patches_fields(db):
    agent = await create_agent(
        db, name="editable", model="claude-sonnet-5", allowed_tools=["Read"]
    )

    updated = await update_agent(
        db,
        agent.id,
        description="new description",
        model="claude-opus-4",
        allowed_tools=["Read", "Write"],
        max_iterations=10,
    )

    assert updated.description == "new description"
    assert updated.model == "claude-opus-4"
    assert json.loads(updated.allowed_tools_json) == ["Read", "Write"]
    assert updated.max_iterations == 10
    assert updated.updated_at >= updated.created_at


@pytest.mark.anyio
async def test_update_agent_reraises_roster_validation(db):
    leaf_ids = [(await _leaf_agent(db, f"u_leaf{i}")).id for i in range(6)]
    agent = await create_agent(
        db, name="u_manager", model="claude-sonnet-5", allowed_tools=[]
    )

    with pytest.raises(ApiError) as exc_info:
        await update_agent(db, agent.id, roster=leaf_ids)

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_update_agent_self_reference_raises(db):
    agent = await create_agent(
        db, name="self_ref", model="claude-sonnet-5", allowed_tools=[]
    )

    with pytest.raises(ApiError) as exc_info:
        await update_agent(db, agent.id, roster=[agent.id])

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_update_agent_not_found_raises(db):
    with pytest.raises(ApiError) as exc_info:
        await update_agent(db, "agt_ghost", description="x")

    assert exc_info.value.status == 404


@pytest.mark.anyio
async def test_update_agent_duplicate_name_raises(db):
    await create_agent(db, name="taken", model="claude-sonnet-5", allowed_tools=[])
    agent = await create_agent(
        db, name="renamable", model="claude-sonnet-5", allowed_tools=[]
    )

    with pytest.raises(ApiError) as exc_info:
        await update_agent(db, agent.id, name="taken")

    assert exc_info.value.status == 422


@pytest.mark.anyio
async def test_delete_agent_removes_row(db):
    agent = await create_agent(
        db, name="deletable", model="claude-sonnet-5", allowed_tools=[]
    )

    await delete_agent(db, agent.id)

    assert await get_agent(db, agent.id) is None


@pytest.mark.anyio
async def test_delete_agent_not_found_raises(db):
    with pytest.raises(ApiError) as exc_info:
        await delete_agent(db, "agt_ghost")

    assert exc_info.value.status == 404


@pytest.mark.anyio
async def test_get_agent_returns_none_when_missing(db):
    assert await get_agent(db, "agt_ghost") is None


@pytest.mark.anyio
async def test_list_agents_returns_all(db):
    await _leaf_agent(db, "list_a")
    await _leaf_agent(db, "list_b")

    agents = await list_agents(db)
    names = {a.name for a in agents}
    assert {"list_a", "list_b"}.issubset(names)


# ---------------------------------------------------------------------------
# From tests/test_agent_model.py
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_agent_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'a.db'}")
    from app.core.config import get_settings; get_settings.cache_clear()
    reset_engine(); await init_db()
    async with SessionLocal() as db:
        db.add(Agent(id="agt_1", name="researcher", model="claude-sonnet-5",
                     allowed_tools_json='["Read","Glob","Grep","WebSearch"]',
                     permission_mode="default", mcp_names_json="[]", roster_json="[]",
                     max_iterations=6))
        await db.commit()
    async with SessionLocal() as db:
        row = (await db.execute(select(Agent).where(Agent.name == "researcher"))).scalar_one()
        assert row.model == "claude-sonnet-5"
        assert row.max_iterations == 6
        assert row.roster_json == "[]"


# ---------------------------------------------------------------------------
# From tests/test_agents_api.py
# ---------------------------------------------------------------------------


@pytest.fixture
async def admin_client(client):
    """An httpx client plus a seeded admin API key."""
    full_key = await _seed_key("t_admin_agents", "k_admin_agents", is_admin=True)
    return {"client": client, "headers": {"Authorization": f"Bearer {full_key}"}}


@pytest.mark.anyio
async def test_agents_crud_flow(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]

    # create
    r = await c.post(
        "/admin/api/agents",
        json={
            "name": "researcher",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read", "Grep"],
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    created = r.json()
    assert created["name"] == "researcher"
    assert created["model"] == "claude-sonnet-5"
    assert created["allowed_tools"] == ["Read", "Grep"]
    assert isinstance(created["roster"], list)
    assert created["roster"] == []
    assert isinstance(created["mcp_names"], list)
    assert created["permission_mode"] == "default"
    assert created["max_iterations"] == 6
    assert created["is_admin_only"] is False
    agent_id = created["id"]
    assert agent_id

    # get by id
    r = await c.get(f"/admin/api/agents/{agent_id}", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["id"] == agent_id

    # list
    r = await c.get("/admin/api/agents", headers=h)
    assert r.status_code == 200, r.text
    names = [a["name"] for a in r.json()]
    assert "researcher" in names

    # patch
    r = await c.patch(
        f"/admin/api/agents/{agent_id}",
        json={"model": "claude-opus-4"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert patched["model"] == "claude-opus-4"
    assert patched["name"] == "researcher"  # unchanged

    # delete
    r = await c.delete(f"/admin/api/agents/{agent_id}", headers=h)
    assert r.status_code == 200, r.text

    # subsequent get -> 404
    r = await c.get(f"/admin/api/agents/{agent_id}", headers=h)
    assert r.status_code == 404


@pytest.mark.anyio
async def test_get_unknown_agent_returns_404(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]
    r = await c.get("/admin/api/agents/agt_missing", headers=h)
    assert r.status_code == 404


@pytest.mark.anyio
async def test_agents_endpoints_reject_non_admin_key(authed_client):
    c = authed_client.client
    h = authed_client.headers

    r = await c.post(
        "/admin/api/agents",
        json={"name": "x", "model": "claude-sonnet-5", "allowed_tools": []},
        headers=h,
    )
    assert r.status_code == 403

    r = await c.get("/admin/api/agents", headers=h)
    assert r.status_code == 403

    r = await c.get("/admin/api/agents/agt_x", headers=h)
    assert r.status_code == 403

    r = await c.patch("/admin/api/agents/agt_x", json={"model": "x"}, headers=h)
    assert r.status_code == 403

    r = await c.delete("/admin/api/agents/agt_x", headers=h)
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# From tests/test_agents_ui.py
# ---------------------------------------------------------------------------


async def _login(client):
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302


def _agent_id_by_name(html: str, name: str) -> str:
    """Extract an agent's id from the list page by matching its row, since
    other (built-in seeded) agents may also be present in the table."""
    m = re.search(
        r"<tr>\s*<td>" + re.escape(name) + r"</td>.*?/admin/agents/(agt_[0-9a-f]+)/edit",
        html,
        re.DOTALL,
    )
    assert m, html
    return m.group(1)


@pytest.mark.anyio
async def test_agents_page_redirects_when_no_cookie(client):
    r = await client.get("/admin/agents")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


@pytest.mark.anyio
async def test_agents_list_and_new_form_render(client):
    await _login(client)

    r = await client.get("/admin/agents")
    assert r.status_code == 200
    assert "<table" in r.text

    r = await client.get("/admin/agents/new")
    assert r.status_code == 200
    assert "<form" in r.text
    # tool checkboxes
    for tool in ["Read", "Write", "Edit", "Bash", "Glob", "Grep", "WebSearch", "WebFetch"]:
        assert f'value="{tool}"' in r.text
    # permission mode select
    assert "permission_mode" in r.text
    assert "acceptEdits" in r.text


@pytest.mark.anyio
async def test_create_agent_via_form_and_list_shows_it(client):
    await _login(client)

    r = await client.post(
        "/admin/agents",
        data={
            "name": "researcher",
            "description": "Does research",
            "model": "claude-sonnet-5",
            "effort": "medium",
            "system_prompt": "You are a researcher.",
            "allowed_tools": ["Read", "Grep"],
            "permission_mode": "default",
            "mcp_names": [],
            "roster": [],
            "max_cost_usd": "1.50",
            "max_iterations": "8",
        },
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/agents"

    r = await client.get("/admin/agents")
    assert r.status_code == 200
    assert "researcher" in r.text


@pytest.mark.anyio
async def test_edit_form_prefilled_and_update_via_form(client):
    await _login(client)

    r = await client.post(
        "/admin/agents",
        data={
            "name": "editor-bot",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read", "Write"],
            "permission_mode": "default",
        },
    )
    assert r.status_code == 302

    r = await client.get("/admin/agents")
    agent_id = _agent_id_by_name(r.text, "editor-bot")

    r = await client.get(f"/admin/agents/{agent_id}/edit")
    assert r.status_code == 200
    assert 'value="editor-bot"' in r.text
    # previously-selected tools should be pre-checked
    assert re.search(r'value="Read"\s+checked', r.text) or re.search(
        r'checked[^>]*value="Read"', r.text
    )

    r = await client.post(
        f"/admin/agents/{agent_id}",
        data={
            "name": "editor-bot",
            "model": "claude-opus-4",
            "allowed_tools": ["Read"],
            "permission_mode": "acceptEdits",
        },
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/agents"

    r = await client.get("/admin/agents")
    assert "claude-opus-4" in r.text


@pytest.mark.anyio
async def test_delete_agent_via_form(client):
    await _login(client)

    r = await client.post(
        "/admin/agents",
        data={
            "name": "throwaway",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read"],
            "permission_mode": "default",
        },
    )
    assert r.status_code == 302

    r = await client.get("/admin/agents")
    agent_id = _agent_id_by_name(r.text, "throwaway")

    r = await client.post(f"/admin/agents/{agent_id}/delete")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/agents"

    r = await client.get("/admin/agents")
    assert "throwaway" not in r.text


@pytest.mark.anyio
async def test_mcp_multiselect_lists_servers(client):
    from app.db.base import SessionLocal
    from app.db.models import McpServer

    async with SessionLocal() as db:
        db.add(McpServer(id="mcp_1", tenant_id=None, name="search-tool", kind="http", url="http://x"))
        await db.commit()

    await _login(client)
    r = await client.get("/admin/agents/new")
    assert r.status_code == 200
    assert "search-tool" in r.text


@pytest.mark.anyio
async def test_roster_multiselect_excludes_self_and_rostered_agents(client):
    await _login(client)

    # base agent with no roster - eligible as a roster member
    r = await client.post(
        "/admin/agents",
        data={
            "name": "leaf-agent",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read"],
            "permission_mode": "default",
        },
    )
    assert r.status_code == 302

    r = await client.get("/admin/agents")
    leaf_id = _agent_id_by_name(r.text, "leaf-agent")

    # manager agent whose roster includes leaf-agent
    r = await client.post(
        "/admin/agents",
        data={
            "name": "manager-agent",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read"],
            "permission_mode": "default",
            "roster": [leaf_id],
        },
    )
    assert r.status_code == 302

    r = await client.get("/admin/agents/new")
    assert r.status_code == 200
    # leaf-agent (no roster) is eligible
    assert "leaf-agent" in r.text
    # manager-agent has a non-empty roster, so it must not appear as a roster option
    assert "manager-agent" not in r.text


# ---------------------------------------------------------------------------
# From tests/test_subagents_mapping.py
# (db fixture renamed to db_subagents to avoid collision with the service db)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_subagents(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'subagents.db'}")
    from app.core.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    await init_db()

    async with SessionLocal() as session:
        yield session


@pytest.mark.anyio
async def test_roster_to_agent_defs_empty_roster_returns_empty_dict(db_subagents):
    assert await roster_to_agent_defs(db_subagents, []) == {}


@pytest.mark.anyio
async def test_roster_to_agent_defs_maps_by_name(db_subagents):
    a1 = await create_agent(
        db_subagents,
        name="researcher",
        model="claude-sonnet-5",
        description="finds things",
        allowed_tools=["Read", "Glob"],
    )
    a2 = await create_agent(
        db_subagents,
        name="writer",
        model="claude-opus-4",
        allowed_tools=["Write"],
    )

    defs = await roster_to_agent_defs(db_subagents, [a1.id, a2.id])

    assert set(defs.keys()) == {"researcher", "writer"}

    researcher = defs["researcher"]
    assert isinstance(researcher, AgentDefinition)
    assert researcher.model == "claude-sonnet-5"
    assert researcher.tools == ["Read", "Glob"]
    assert researcher.description == "finds things"

    writer = defs["writer"]
    assert writer.model == "claude-opus-4"
    assert writer.tools == ["Write"]


# ---------------------------------------------------------------------------
# From tests/test_build_run_config_agent.py
# (db fixture renamed to db_run_config to avoid collision with the service db)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_run_config(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'run_config.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    await init_db()

    async with SessionLocal() as session:
        yield session


@pytest.mark.anyio
async def test_build_run_config_uses_agent_when_session_has_agent_id(db_run_config):
    roster_member = await create_agent(
        db_run_config, name="helper", model="claude-sonnet-5", allowed_tools=["Read"]
    )
    agent = await create_agent(
        db_run_config,
        name="lead",
        model="claude-opus-4",
        effort="high",
        system_prompt="You are the lead.",
        allowed_tools=["Read", "Write"],
        permission_mode="acceptEdits",
        roster=[roster_member.id],
    )

    session = Session(
        id="sess_agent1",
        tenant_id="t_test",
        profile="agent",
        agent_id=agent.id,
        overrides_json="{}",
        mcp_names_json="[]",
        workspace_path="/tmp/ws/sess_agent1",
        status="active",
    )
    db_run_config.add(session)
    await db_run_config.commit()

    cfg = await build_run_config(db_run_config, "t_test", session, "hello")

    assert cfg.model == "claude-opus-4"
    assert cfg.allowed_tools == ["Read", "Write"]
    assert cfg.permission_mode == "acceptEdits"
    assert cfg.system_prompt == "You are the lead."
    assert cfg.effort == "high"
    assert set(cfg.agents.keys()) == {"helper"}
    assert cfg.max_turns == 30
    assert cfg.resume is None


@pytest.mark.anyio
async def test_bypass_permissions_overrides_permission_mode(db_run_config):
    agent = await create_agent(
        db_run_config,
        name="builder",
        model="claude-sonnet-5",
        allowed_tools=["Read", "Write", "Bash"],
        permission_mode="acceptEdits",
        bypass_permissions=True,
    )
    session = Session(
        id="sess_bypass",
        tenant_id="t_test",
        profile="agent",
        agent_id=agent.id,
        overrides_json="{}",
        mcp_names_json="[]",
        workspace_path="/tmp/ws/sess_bypass",
        status="active",
    )
    db_run_config.add(session)
    await db_run_config.commit()

    cfg = await build_run_config(db_run_config, "t_test", session, "go")
    assert cfg.permission_mode == "bypassPermissions"


@pytest.mark.anyio
async def test_system_suffix_appended_to_agent_prompt(db_run_config):
    agent = await create_agent(
        db_run_config,
        name="lead2",
        model="claude-sonnet-5",
        system_prompt="Base prompt.",
        allowed_tools=["Read"],
    )
    session = Session(
        id="sess_suffix",
        tenant_id="t_test",
        profile="agent",
        agent_id=agent.id,
        overrides_json="{}",
        mcp_names_json="[]",
        workspace_path="/tmp/ws/sess_suffix",
        status="active",
    )
    db_run_config.add(session)
    await db_run_config.commit()

    cfg = await build_run_config(db_run_config, "t_test", session, "go", system_suffix="\n\nAUTONOMY")
    assert cfg.system_prompt == "Base prompt.\n\nAUTONOMY"


@pytest.mark.anyio
async def test_build_run_config_without_agent_raises(db_run_config):
    session = Session(
        id="sess_noagent",
        tenant_id="t_test",
        profile="none",
        agent_id=None,
        overrides_json="{}",
        mcp_names_json="[]",
        workspace_path="/tmp/ws/sess_noagent",
        status="active",
    )
    db_run_config.add(session)
    await db_run_config.commit()

    with pytest.raises(ApiError):
        await build_run_config(db_run_config, "t_test", session, "hello")


def test_run_config_agents_and_effort_flow_into_options():
    runtime = AgentRuntime()
    cfg = RunConfig(
        prompt="hi",
        cwd="/tmp",
        system_prompt=None,
        allowed_tools=["Read"],
        permission_mode="default",
        mcp_servers={},
        model=None,
        max_turns=10,
        resume=None,
        timeout_s=60,
        agents={"helper": AgentDefinition(description="d", prompt="p")},
        effort="high",
    )

    options = runtime._options(cfg)

    assert options.agents == {"helper": AgentDefinition(description="d", prompt="p")}
    assert options.effort == "high"


def test_run_config_omits_agents_and_effort_when_none():
    runtime = AgentRuntime()
    cfg = RunConfig(
        prompt="hi",
        cwd="/tmp",
        system_prompt=None,
        allowed_tools=["Read"],
        permission_mode="default",
        mcp_servers={},
        model=None,
        max_turns=10,
        resume=None,
        timeout_s=60,
    )

    options = runtime._options(cfg)

    assert options.agents is None
    assert options.effort is None
