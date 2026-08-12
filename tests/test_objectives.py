import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.base import SessionLocal, init_db, reset_engine
from app.db.models import ApiKey, Event, Objective, Tenant
from app.services.agent.evaluator import Evaluator, _parse_verdict
from app.services.agent.objective_runner import run_objective
from app.services.agents import create_agent
from app.services.sessions import create_session_record
from tests.conftest import _seed_key
from tests.fakes import FakeAgentRuntime


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Objectives route (from test_objectives_route.py)
# ---------------------------------------------------------------------------


def _events() -> list[dict]:
    """Init -> text -> result, where the result text is itself a satisfied
    verdict JSON. The same `FakeAgentRuntime` serves both the orchestrator's
    ACT call (`run_session_message`) and the `Evaluator.grade` call, so
    feeding a verdict JSON as the "draft" result also makes the evaluator
    parse `satisfied=true` and stop the loop after one iteration."""
    verdict = json.dumps(
        {"satisfied": True, "score": 1.0, "gaps": [], "reasoning": "ok"}
    )
    return [
        {"type": "init", "session_id": "sdk-obj-route-1"},
        {"type": "text", "text": "draft"},
        {
            "type": "result",
            "subtype": "success",
            "result": verdict,
            "session_id": "sdk-obj-route-1",
            "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 0},
            "cost_usd": 0.01,
            "duration_ms": 10,
            "num_turns": 1,
        },
    ]


async def _seed_agent(authed_client) -> str:
    """Create an Agent via the admin API (agents are global, not
    tenant-scoped) and return its id."""
    admin_key = await _seed_key("t_admin_objectives", "k_admin_objectives", is_admin=True)
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    r = await authed_client.client.post(
        "/admin/api/agents",
        json={
            "name": "obj-writer",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read", "Write"],
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _poll_terminal(authed_client, objective_id: str) -> dict:
    body = None
    for _ in range(60):
        r = await authed_client.client.get(
            f"/v1/objectives/{objective_id}", headers=authed_client.headers
        )
        assert r.status_code == 200
        body = r.json()
        if body["status"] not in ("queued", "running"):
            break
        await asyncio.sleep(0.05)
    return body


@pytest.mark.anyio
async def test_submit_objective_runs_to_success(authed_client):
    agent_id = await _seed_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Write a haiku", "rubric": "Must be a haiku"},
        headers=authed_client.headers,
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["objective_id"].startswith("obj_")
    objective_id = body["objective_id"]

    final = await _poll_terminal(authed_client, objective_id)
    assert final["status"] == "succeeded", final
    assert final["iterations_done"] >= 1
    assert final["agent_id"] == agent_id
    assert final["goal"] == "Write a haiku"
    assert final["rubric"] == "Must be a haiku"
    assert final["session_id"] is not None
    assert final["finished_at"] is not None


@pytest.mark.anyio
async def test_list_objectives_returns_callers_objectives(authed_client):
    agent_id = await _seed_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Write a limerick", "rubric": "Must rhyme"},
        headers=authed_client.headers,
    )
    objective_id = r.json()["objective_id"]

    r = await authed_client.client.get("/v1/objectives", headers=authed_client.headers)
    assert r.status_code == 200
    ids = {o["objective_id"] for o in r.json()}
    assert objective_id in ids


@pytest.mark.anyio
async def test_get_objective_from_other_tenant_returns_404(authed_client):
    agent_id = await _seed_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Write a poem", "rubric": "Must be nice"},
        headers=authed_client.headers,
    )
    objective_id = r.json()["objective_id"]

    other_key = await _seed_key("t_other_objectives", "k_other_objectives", is_admin=False)
    other_headers = {"Authorization": f"Bearer {other_key}"}

    r = await authed_client.client.get(
        f"/v1/objectives/{objective_id}", headers=other_headers
    )
    assert r.status_code == 404


async def _seed_admin_only_agent(authed_client) -> str:
    """Create an is_admin_only Agent via the admin API (mirrors the seeded
    `trusted` agent, e.g. one with Bash access) and return its id."""
    admin_key = await _seed_key("t_admin_objectives_gate", "k_admin_objectives_gate", is_admin=True)
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    r = await authed_client.client.post(
        "/admin/api/agents",
        json={
            "name": "obj-admin-only",
            "model": "claude-sonnet-5",
            "allowed_tools": ["Read", "Write", "Bash"],
            "is_admin_only": True,
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"], admin_key


@pytest.mark.anyio
async def test_submit_objective_admin_only_agent_rejects_non_admin_key(authed_client):
    """CRITICAL regression: a normal tenant key must not be able to launch an
    objective driven by an is_admin_only agent. The session path already
    enforces this gate; the objective path must too."""
    agent_id, _admin_key = await _seed_admin_only_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Do something", "rubric": "n/a"},
        headers=authed_client.headers,
    )
    assert r.status_code == 403, r.text


@pytest.mark.anyio
async def test_submit_objective_admin_only_agent_allows_admin_key(authed_client):
    agent_id, admin_key = await _seed_admin_only_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Do something", "rubric": "n/a"},
        headers=admin_headers,
    )
    assert r.status_code == 202, r.text


@pytest.mark.anyio
async def test_submit_objective_unknown_agent_returns_404(authed_client):
    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": "agt_doesnotexist", "goal": "goal", "rubric": "rubric"},
        headers=authed_client.headers,
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_objective_events_stream_replays_events(authed_client):
    agent_id = await _seed_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Write a haiku", "rubric": "Must be a haiku"},
        headers=authed_client.headers,
    )
    objective_id = r.json()["objective_id"]

    final = await _poll_terminal(authed_client, objective_id)
    assert final["status"] == "succeeded", final

    r = await authed_client.client.get(
        f"/v1/objectives/{objective_id}/events", headers=authed_client.headers
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: objective.iteration_started" in r.text
    assert "event: objective.evaluation" in r.text
    assert "event: objective.finished" in r.text


@pytest.mark.anyio
async def test_objective_events_from_other_tenant_returns_404(authed_client):
    agent_id = await _seed_agent(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        "/v1/objectives",
        json={"agent": agent_id, "goal": "Write a haiku", "rubric": "Must be a haiku"},
        headers=authed_client.headers,
    )
    objective_id = r.json()["objective_id"]

    other_key = await _seed_key("t_other_objectives_events", "k_other_objectives_events", is_admin=False)
    other_headers = {"Authorization": f"Bearer {other_key}"}

    r = await authed_client.client.get(
        f"/v1/objectives/{objective_id}/events", headers=other_headers
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Objective loop (from test_objective_loop.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'objective_loop.db'}")
    monkeypatch.setenv("WORKSPACE_ROOT", str(tmp_path / "ws"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    reset_engine()
    await init_db()

    async with SessionLocal() as session:
        yield session


def _loop_events() -> list[dict]:
    return [
        {"type": "init", "session_id": "sdk-obj-1"},
        {"type": "text", "text": "draft"},
        {
            "type": "result",
            "subtype": "success",
            "result": "draft",
            "session_id": "sdk-obj-1",
            "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 0},
            "cost_usd": 0.01,
            "duration_ms": 10,
            "num_turns": 1,
        },
    ]


class FakeEvaluatorEventuallySatisfied:
    """Not satisfied on the first call, satisfied on the second."""

    def __init__(self) -> None:
        self.calls = 0

    async def grade(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"satisfied": False, "score": 0.1, "gaps": ["x"], "reasoning": "r"}
        return {"satisfied": True, "score": 1.0, "gaps": [], "reasoning": "ok"}


class FakeEvaluatorNeverSatisfied:
    async def grade(self, **kwargs):
        return {"satisfied": False, "score": 0.1, "gaps": ["x"], "reasoning": "r"}


async def _seed(db, *, tenant_id: str, max_cost_usd=None, max_iterations=6):
    if await db.get(Tenant, tenant_id) is None:
        db.add(Tenant(id=tenant_id, name=tenant_id))
    db.add(
        ApiKey(
            id=f"k_{tenant_id}",
            tenant_id=tenant_id,
            key_hash=f"hash_{tenant_id}",
            prefix="sk-test",
            name="test key",
        )
    )
    await db.commit()

    agent = await create_agent(
        db,
        name=f"writer_{tenant_id}",
        model="claude-sonnet-5",
        allowed_tools=["Read", "Write"],
    )

    obj = Objective(
        id=f"obj_{tenant_id}",
        tenant_id=tenant_id,
        api_key_id=f"k_{tenant_id}",
        agent_id=agent.id,
        goal="Write a haiku",
        rubric="Must be a haiku",
        max_cost_usd=max_cost_usd,
        max_iterations=max_iterations,
    )
    db.add(obj)
    await db.commit()
    await db.refresh(obj)
    return obj


async def _events_for_session(session_id: str) -> list[Event]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(Event).where(Event.session_id == session_id).order_by(Event.seq)
        )
        return list(result.scalars().all())


@pytest.mark.anyio
async def test_run_objective_succeeds_after_second_iteration(db):
    obj = await _seed(db, tenant_id="t_succeed")

    app_obj = SimpleNamespace(state=SimpleNamespace(runtime=FakeAgentRuntime(_loop_events())))
    fake_evaluator = FakeEvaluatorEventuallySatisfied()

    await run_objective(app_obj, obj.id, evaluator=fake_evaluator)

    async with SessionLocal() as check_db:
        refreshed = await check_db.get(Objective, obj.id)

    assert refreshed.status == "succeeded"
    assert refreshed.iterations_done == 2
    assert refreshed.result_text == "draft"
    assert refreshed.session_id is not None
    assert refreshed.finished_at is not None

    events = await _events_for_session(refreshed.session_id)
    types = [e.type for e in events]
    assert types.count("objective.iteration_started") == 2
    assert types.count("objective.evaluation") == 2
    assert types.count("objective.finished") == 1

    finished_payload = json.loads(events[-1].payload_json)
    assert finished_payload == {"status": "succeeded"}


def _blocked_events() -> list[dict]:
    return [
        {"type": "init", "session_id": "sdk-obj-b"},
        {
            "type": "tool_use",
            "id": "tu1",
            "name": "Bash",
            "input": {"command": "git init", "description": "init repo"},
        },
        {
            "type": "tool_result",
            "tool_use_id": "tu1",
            "is_error": True,
            "content": "This command requires approval",
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "could not run git",
            "session_id": "sdk-obj-b",
            "usage": {"input_tokens": 5, "output_tokens": 2, "cache_read_tokens": 0},
            "cost_usd": 0.01,
            "duration_ms": 10,
            "num_turns": 1,
        },
    ]


@pytest.mark.anyio
async def test_run_objective_stops_on_permission_block(db):
    # max_iterations=5, but a permission denial should stop the loop after the
    # first iteration instead of grinding into the same wall five times.
    obj = await _seed(db, tenant_id="t_blocked", max_iterations=5)

    app_obj = SimpleNamespace(state=SimpleNamespace(runtime=FakeAgentRuntime(_blocked_events())))

    await run_objective(app_obj, obj.id, evaluator=FakeEvaluatorNeverSatisfied())

    async with SessionLocal() as check_db:
        refreshed = await check_db.get(Objective, obj.id)

    assert refreshed.status == "permission_blocked"
    assert refreshed.iterations_done == 1
    assert "git init" in (refreshed.result_text or "")

    events = await _events_for_session(refreshed.session_id)
    types = [e.type for e in events]
    assert "objective.blocked" in types
    assert json.loads(events[-1].payload_json) == {"status": "permission_blocked"}


@pytest.mark.anyio
async def test_run_objective_hits_max_iterations(db):
    obj = await _seed(db, tenant_id="t_maxiter", max_iterations=2)

    app_obj = SimpleNamespace(state=SimpleNamespace(runtime=FakeAgentRuntime(_loop_events())))
    fake_evaluator = FakeEvaluatorNeverSatisfied()

    await run_objective(app_obj, obj.id, evaluator=fake_evaluator)

    async with SessionLocal() as check_db:
        refreshed = await check_db.get(Objective, obj.id)

    assert refreshed.status == "max_iterations"
    assert refreshed.iterations_done == 2
    assert refreshed.finished_at is not None

    events = await _events_for_session(refreshed.session_id)
    types = [e.type for e in events]
    assert types.count("objective.iteration_started") == 2
    assert types.count("objective.evaluation") == 2
    assert types.count("objective.finished") == 1


# ---------------------------------------------------------------------------
# Objectives UI (from test_objectives_ui.py)
# ---------------------------------------------------------------------------


async def _seed_objective(*, tenant_id: str = "t_obj_ui") -> str:
    """Seed a tenant/key/agent, a working Session with a small trace of
    Event rows (iteration_started -> text -> evaluation -> finished), and an
    Objective row pointing at that session. Returns the objective id."""
    await _seed_key(tenant_id, f"k_{tenant_id}", is_admin=False)

    async with SessionLocal() as db:
        agent = await create_agent(
            db,
            name=f"writer_{tenant_id}",
            model="claude-sonnet-5",
            allowed_tools=["Read", "Write"],
        )

        session = await create_session_record(
            db,
            tenant_id=tenant_id,
            agent_id=agent.id,
            allow_admin_only=True,
            title="objective working session",
        )

        obj = Objective(
            id=f"obj_{tenant_id}",
            tenant_id=tenant_id,
            api_key_id=f"k_{tenant_id}",
            agent_id=agent.id,
            goal="Write a haiku",
            rubric="Must be a haiku",
            status="succeeded",
            max_iterations=6,
            iterations_done=1,
            cost_usd=0.01,
            result_text="an old silent pond...",
            session_id=session.id,
        )
        db.add(obj)
        await db.commit()

        events = [
            Event(
                session_id=session.id,
                seq=1,
                type="objective.iteration_started",
                payload_json=json.dumps({"iteration": 1}),
            ),
            Event(
                session_id=session.id,
                seq=2,
                type="text",
                payload_json=json.dumps({"text": "an old silent pond..."}),
            ),
            Event(
                session_id=session.id,
                seq=3,
                type="objective.evaluation",
                payload_json=json.dumps(
                    {"satisfied": True, "score": 1.0, "gaps": [], "reasoning": "looks good"}
                ),
            ),
            Event(
                session_id=session.id,
                seq=4,
                type="objective.finished",
                payload_json=json.dumps({"status": "succeeded"}),
            ),
        ]
        for e in events:
            db.add(e)
        await db.commit()

    return obj.id


@pytest.mark.anyio
async def test_objectives_list_redirects_when_no_cookie(client):
    r = await client.get("/admin/objectives")
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/login"


@pytest.mark.anyio
async def test_objectives_list_shows_seeded_objective(client):
    objective_id = await _seed_objective()

    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.get("/admin/objectives")
    assert r.status_code == 200
    assert "<table" in r.text
    assert objective_id in r.text
    assert "succeeded" in r.text.lower()


@pytest.mark.anyio
async def test_objective_detail_shows_trace(client):
    objective_id = await _seed_objective()

    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.get(f"/admin/objectives/{objective_id}")
    assert r.status_code == 200
    assert "Write a haiku" in r.text
    assert "succeeded" in r.text.lower()
    # Evaluation payload surfaced.
    assert "1.0" in r.text or "1" in r.text
    assert "looks good" in r.text
    # Assistant text from the driven session's `text` event.
    assert "an old silent pond" in r.text


@pytest.mark.anyio
async def test_objective_detail_empty_state_when_no_session(client):
    tenant_id = "t_obj_ui_empty"
    await _seed_key(tenant_id, f"k_{tenant_id}", is_admin=False)

    async with SessionLocal() as db:
        agent = await create_agent(
            db,
            name=f"writer_{tenant_id}",
            model="claude-sonnet-5",
            allowed_tools=["Read"],
        )
        obj = Objective(
            id=f"obj_{tenant_id}",
            tenant_id=tenant_id,
            api_key_id=f"k_{tenant_id}",
            agent_id=agent.id,
            goal="Do a thing",
            rubric="Rubric",
            status="running",
            max_iterations=6,
        )
        db.add(obj)
        await db.commit()

    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.get(f"/admin/objectives/{obj.id}")
    assert r.status_code == 200
    assert "no" in r.text.lower()  # empty-state copy


@pytest.mark.anyio
async def test_objective_detail_200_not_found_for_unknown_id(client):
    """Mirrors session_detail_page/completion_detail_page: an unknown id
    renders the same template with a 200 and a "not found" message rather
    than a 404, so the admin UI never hard-errors on a stale link."""
    r = await client.post("/admin/login", data={"password": "admin"})
    assert r.status_code == 302

    r = await client.get("/admin/objectives/obj_does_not_exist")
    assert r.status_code == 200
    assert "not found" in r.text.lower()


# ---------------------------------------------------------------------------
# Objective model (from test_objective_model.py)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_objective_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path/'a.db'}")
    from app.core.config import get_settings; get_settings.cache_clear()
    reset_engine(); await init_db()
    async with SessionLocal() as db:
        db.add(Objective(
            id="obj_1",
            tenant_id="ten_1",
            api_key_id="key_1",
            agent_id="agt_1",
            goal="Write a summary of the repo",
            rubric="Summary covers architecture and key modules",
            max_cost_usd=5.0,
            max_iterations=10,
        ))
        await db.commit()
    async with SessionLocal() as db:
        row = (await db.execute(select(Objective).where(Objective.id == "obj_1"))).scalar_one()
        assert row.status == "queued"
        assert row.iterations_done == 0
        assert row.cost_usd == 0.0
        assert row.max_iterations == 10
        assert row.max_cost_usd == 5.0
        assert row.result_text is None
        assert row.session_id is None
        assert row.finished_at is None
        assert row.created_at is not None


# ---------------------------------------------------------------------------
# Evaluator (from test_evaluator.py)
# ---------------------------------------------------------------------------


def _result_event(text: str) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": text,
        "session_id": "e1",
        "usage": {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0},
        "cost_usd": 0.0,
        "duration_ms": 0,
        "num_turns": 1,
    }


class TestGradeHappyPath:
    @pytest.mark.anyio
    async def test_grade_parses_satisfied_verdict(self):
        text = 'Looks good: {"satisfied": true, "score": 0.9, "gaps": [], "reasoning": "ok"}'
        runtime = FakeAgentRuntime([_result_event(text)])
        evaluator = Evaluator(runtime)

        verdict = await evaluator.grade(
            goal="Build a thing",
            rubric="Must work",
            artifact_text="I built the thing",
            file_manifest=[{"path": "main.py", "size": 10}],
            cwd="/tmp/ws",
        )

        assert verdict["satisfied"] is True
        assert verdict["score"] == 0.9
        assert verdict["gaps"] == []
        assert verdict["reasoning"] == "ok"


class TestParseVerdictFallback:
    def test_no_json_returns_error_fallback(self):
        verdict = _parse_verdict("I refuse to answer in JSON today.")

        assert verdict["satisfied"] is False
        assert verdict["score"] == 0.0
        # Evaluator failure is flagged via `error`, not surfaced as a fake gap.
        assert verdict["error"] is True
        assert verdict["gaps"] == []
        assert verdict["reasoning"] == "I refuse to answer in JSON today."

    def test_malformed_json_returns_error_fallback(self):
        verdict = _parse_verdict("here you go: {not: valid json}")

        assert verdict["satisfied"] is False
        assert verdict["score"] == 0.0
        assert verdict["error"] is True
        assert verdict["gaps"] == []

    def test_valid_verdict_has_error_false(self):
        verdict = _parse_verdict('{"satisfied": true, "score": 1, "gaps": [], "reasoning": "ok"}')

        assert verdict["satisfied"] is True
        assert verdict["error"] is False

    def test_strips_markdown_fences(self):
        verdict = _parse_verdict('```json\n{"satisfied": false, "score": 0.5, "gaps": ["x"], "reasoning": "r"}\n```')

        assert verdict["error"] is False
        assert verdict["satisfied"] is False
        assert verdict["gaps"] == ["x"]
