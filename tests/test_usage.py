import pytest

import app.services.models as models_module
from app.services import claude_usage
from tests.conftest import _seed_key

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clear_cache():
    claude_usage.invalidate_cache()
    yield
    claude_usage.invalidate_cache()


@pytest.fixture
async def admin_client(client):
    full_key = await _seed_key("t_admin_models", "k_admin_models", is_admin=True)
    return {"client": client, "headers": {"Authorization": f"Bearer {full_key}"}}


async def _seed_usage(
    tenant_id: str,
    api_key_id: str,
    session_id: str | None,
    *,
    cost_usd: float,
    input_tokens: int,
    output_tokens: int,
):
    from app.db.base import SessionLocal
    from app.db.models import Usage

    async with SessionLocal() as db:
        db.add(
            Usage(
                tenant_id=tenant_id,
                api_key_id=api_key_id,
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=0,
                cost_usd=cost_usd,
                duration_ms=100,
                num_turns=1,
            )
        )
        await db.commit()


@pytest.mark.anyio
async def test_usage_aggregates_callers_rows_by_session(authed_client):
    await _seed_usage(
        authed_client.tenant_id,
        authed_client.key_id,
        "sess_a",
        cost_usd=0.5,
        input_tokens=10,
        output_tokens=20,
    )
    await _seed_usage(
        authed_client.tenant_id,
        authed_client.key_id,
        "sess_b",
        cost_usd=0.25,
        input_tokens=5,
        output_tokens=7,
    )

    other_key_id = "k_other"
    await _seed_key("t_other", other_key_id, is_admin=False)
    await _seed_usage(
        "t_other",
        other_key_id,
        "sess_c",
        cost_usd=100.0,
        input_tokens=999,
        output_tokens=999,
    )

    r = await authed_client.client.get("/v1/usage", headers=authed_client.headers)

    assert r.status_code == 200
    body = r.json()
    assert body["total_cost_usd"] == pytest.approx(0.75)
    assert body["input_tokens"] == 15
    assert body["output_tokens"] == 27
    assert body["runs"] == 2

    by_session = {row["session_id"]: row for row in body["by_session"]}
    assert set(by_session.keys()) == {"sess_a", "sess_b"}
    assert by_session["sess_a"]["cost_usd"] == pytest.approx(0.5)
    assert by_session["sess_a"]["runs"] == 1
    assert by_session["sess_b"]["cost_usd"] == pytest.approx(0.25)
    assert by_session["sess_b"]["runs"] == 1


@pytest.mark.anyio
async def test_usage_with_no_rows_returns_zeroed_totals(authed_client):
    r = await authed_client.client.get("/v1/usage", headers=authed_client.headers)

    assert r.status_code == 200
    body = r.json()
    assert body["total_cost_usd"] == 0.0
    assert body["input_tokens"] == 0
    assert body["output_tokens"] == 0
    assert body["runs"] == 0
    assert body["by_session"] == []


@pytest.mark.anyio
async def test_usage_filters_by_date_range(authed_client):
    from datetime import datetime, timedelta, timezone

    from app.db.base import SessionLocal
    from app.db.models import Usage

    now = datetime.now(timezone.utc)
    old = now - timedelta(days=10)

    async with SessionLocal() as db:
        db.add(
            Usage(
                tenant_id=authed_client.tenant_id,
                api_key_id=authed_client.key_id,
                session_id="sess_old",
                input_tokens=1,
                output_tokens=1,
                cache_read_tokens=0,
                cost_usd=1.0,
                duration_ms=10,
                num_turns=1,
                created_at=old,
            )
        )
        db.add(
            Usage(
                tenant_id=authed_client.tenant_id,
                api_key_id=authed_client.key_id,
                session_id="sess_recent",
                input_tokens=2,
                output_tokens=2,
                cache_read_tokens=0,
                cost_usd=2.0,
                duration_ms=10,
                num_turns=1,
                created_at=now,
            )
        )
        await db.commit()

    from_date = (now - timedelta(days=1)).date().isoformat()
    r = await authed_client.client.get(
        f"/v1/usage?from={from_date}", headers=authed_client.headers
    )

    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == 1
    assert body["total_cost_usd"] == pytest.approx(2.0)


@pytest.mark.anyio
async def test_usage_invalid_date_returns_422(authed_client):
    r = await authed_client.client.get(
        "/v1/usage?from=not-a-date", headers=authed_client.headers
    )

    assert r.status_code == 422


@pytest.mark.anyio
async def test_plan_usage_endpoint(authed_client, monkeypatch):
    from app.services import claude_usage

    async def fake_plan():
        return {"available": True, "session": {"percent": 3, "resets_at": None},
                "weekly": {"percent": 5, "resets_at": None}, "scoped": []}

    monkeypatch.setattr(claude_usage, "get_plan_usage", fake_plan)
    r = await authed_client.client.get("/v1/usage/plan", headers=authed_client.headers)
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True and body["weekly"]["percent"] == 5


@pytest.mark.anyio
async def test_plan_usage_requires_key(authed_client):
    r = await authed_client.client.get("/v1/usage/plan")
    assert r.status_code == 401


_SAMPLE = {
    "five_hour": {"utilization": 3.0, "resets_at": "2026-08-12T14:59:59+00:00"},
    "seven_day": {"utilization": 5.4, "resets_at": "2026-08-19T06:00:00+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "extra_usage": {"is_enabled": False, "utilization": None, "monthly_limit": None},
    "limits": [
        {"kind": "session", "group": "session", "percent": 3},
        {"kind": "weekly_all", "group": "weekly", "percent": 5},
        {
            "kind": "weekly_scoped",
            "group": "weekly",
            "percent": 5,
            "resets_at": "2026-08-19T06:00:00+00:00",
            "severity": "normal",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        },
    ],
}


def test_normalize_shapes_the_payload():
    n = claude_usage._normalize(_SAMPLE)
    assert n["available"] is True
    assert n["session"] == {"percent": 3, "resets_at": "2026-08-12T14:59:59+00:00"}
    assert n["weekly"]["percent"] == 5  # 5.4 rounds to 5
    assert n["weekly_opus"] is None
    assert n["scoped"] == [
        {
            "label": "Fable",
            "percent": 5,
            "resets_at": "2026-08-19T06:00:00+00:00",
            "severity": "normal",
        }
    ]


async def test_get_plan_usage_success(monkeypatch):
    async def fake_fetch():
        return claude_usage._normalize(_SAMPLE)

    monkeypatch.setattr(claude_usage, "_fetch", fake_fetch)
    r = await claude_usage.get_plan_usage()
    assert r["available"] is True and r["session"]["percent"] == 3


async def test_get_plan_usage_unavailable(monkeypatch):
    async def fake_fetch():
        return None

    monkeypatch.setattr(claude_usage, "_fetch", fake_fetch)
    r = await claude_usage.get_plan_usage()
    assert r == {"available": False}


async def test_usage_page_renders_plan(client, monkeypatch):
    async def fake_plan():
        return claude_usage._normalize(_SAMPLE)

    monkeypatch.setattr(claude_usage, "get_plan_usage", fake_plan)
    await client.post("/admin/login", data={"password": "admin"})
    r = await client.get("/admin/usage")
    assert r.status_code == 200
    assert "Plan usage" in r.text and "Weekly — Fable" in r.text


async def test_models_endpoint_returns_catalog_and_effort(admin_client):
    c = admin_client["client"]
    h = admin_client["headers"]

    r = await c.get("/admin/api/models", headers=h)
    assert r.status_code == 200
    body = r.json()

    assert body["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert isinstance(body["models"], list) and body["models"]
    # Each entry is {id, label} suitable for a searchable <datalist>.
    for m in body["models"]:
        assert set(m) == {"id", "label"}


async def test_models_endpoint_uses_fallback_when_live_disabled(admin_client):
    # conftest sets MODELS_LIVE_FETCH=false, so the curated fallback is served.
    c = admin_client["client"]
    h = admin_client["headers"]

    r = await c.get("/admin/api/models", headers=h)
    ids = {m["id"] for m in r.json()["models"]}
    assert {"claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"} <= ids


async def test_get_models_prefers_live_when_available(monkeypatch):
    # With live fetch on and a stubbed API result, the live list wins over fallback.
    monkeypatch.setenv("MODELS_LIVE_FETCH", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    models_module._CACHE["models"] = None
    models_module._CACHE["expires"] = 0.0

    async def _fake_live():
        return [{"id": "claude-live-1", "label": "Live One"}]

    monkeypatch.setattr(models_module, "_fetch_live", _fake_live)

    result = await models_module.get_models()
    assert result == [{"id": "claude-live-1", "label": "Live One"}]

    get_settings.cache_clear()
    models_module._CACHE["models"] = None
    models_module._CACHE["expires"] = 0.0


async def test_get_models_falls_back_when_live_returns_none(monkeypatch):
    monkeypatch.setenv("MODELS_LIVE_FETCH", "true")
    from app.core.config import get_settings

    get_settings.cache_clear()
    models_module._CACHE["models"] = None
    models_module._CACHE["expires"] = 0.0

    async def _no_live():
        return None

    monkeypatch.setattr(models_module, "_fetch_live", _no_live)

    result = await models_module.get_models()
    ids = {m["id"] for m in result}
    assert "claude-opus-5" in ids

    get_settings.cache_clear()
    models_module._CACHE["models"] = None
    models_module._CACHE["expires"] = 0.0
