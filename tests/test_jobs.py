import asyncio
import hashlib
import hmac
import json

import httpx
import pytest

from tests.conftest import _default_agent_id

from sqlalchemy import select

from app.db.base import SessionLocal
from app.db.models import Usage
from tests.conftest import _seed_key
from tests.fakes import FakeAgentRuntime


def _events() -> list[dict]:
    return [
        {"type": "init", "session_id": "sdk-job-1"},
        {"type": "text", "text": "working on it"},
        {
            "type": "result",
            "subtype": "success",
            "result": "done async",
            "session_id": "sdk-job-1",
            "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 0},
            "cost_usd": 0.02,
            "duration_ms": 50,
            "num_turns": 1,
        },
    ]


async def _create_session(authed_client) -> str:
    r = await authed_client.client.post(
        "/v1/sessions", json={"agent": await _default_agent_id()}, headers=authed_client.headers
    )
    assert r.status_code == 200
    return r.json()["session_id"]


@pytest.fixture
def recorded_calls():
    return []


@pytest.fixture(autouse=True)
def _patch_webhook_post(monkeypatch, recorded_calls):
    """Intercept only the real outbound `httpx.AsyncClient` used by
    `fire_webhook` (default transport), leaving the test's own ASGI-backed
    client (`authed_client.client`, built with `ASGITransport`) untouched so
    it keeps hitting the in-process app normally."""

    original_post = httpx.AsyncClient.post

    async def fake_post(self, url, **kwargs):
        if isinstance(self._transport, httpx.ASGITransport):
            return await original_post(self, url, **kwargs)
        recorded_calls.append(
            {"url": url, "headers": kwargs.get("headers"), "body": kwargs.get("content")}
        )
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)


@pytest.mark.anyio
async def test_async_message_runs_job_and_fires_signed_webhook(
    authed_client, recorded_calls
):
    # Admin key to configure the tenant's webhook.
    admin_key = await _seed_key("t_admin_jobs", "k_admin_jobs", is_admin=True)
    admin_headers = {"Authorization": f"Bearer {admin_key}"}

    webhook_url = "https://example.test/hooks/jobs"
    webhook_secret = "s3cr3t"
    r = await authed_client.client.put(
        f"/admin/api/webhooks/{authed_client.tenant_id}",
        json={"url": webhook_url, "secret": webhook_secret},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"tenant_id": authed_client.tenant_id, "url": webhook_url}

    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hello async", "mode": "async"},
        headers=authed_client.headers,
    )
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    assert job_id.startswith("job_")
    assert r.json()["status"] == "queued"

    # Poll GET /v1/jobs/{id} until the background task finishes.
    status = None
    body = None
    for _ in range(40):
        r = await authed_client.client.get(
            f"/v1/jobs/{job_id}", headers=authed_client.headers
        )
        assert r.status_code == 200
        body = r.json()
        status = body["status"]
        if status in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)

    assert status == "succeeded", body
    assert body["result"]["result"] == "done async"
    assert body["finished_at"] is not None

    async with SessionLocal() as db:
        usage_rows = (
            (await db.execute(select(Usage).where(Usage.session_id == sid)))
            .scalars()
            .all()
        )
    assert len(usage_rows) == 1
    assert usage_rows[0].input_tokens == 7
    assert usage_rows[0].cost_usd == pytest.approx(0.02)

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call["url"] == webhook_url

    sig_header = call["headers"]["X-Signature"]
    assert sig_header.startswith("sha256=")
    expected = hmac.new(
        webhook_secret.encode(), call["body"], hashlib.sha256
    ).hexdigest()
    assert sig_header == f"sha256={expected}"

    payload = json.loads(call["body"])
    assert payload["job_id"] == job_id
    assert payload["status"] == "succeeded"
    assert payload["session_id"] == sid
    assert payload["result"]["result"] == "done async"


@pytest.mark.anyio
async def test_async_message_without_webhook_config_does_not_call_post(
    authed_client, recorded_calls
):
    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "no webhook", "mode": "async"},
        headers=authed_client.headers,
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]

    status = None
    for _ in range(40):
        r = await authed_client.client.get(
            f"/v1/jobs/{job_id}", headers=authed_client.headers
        )
        status = r.json()["status"]
        if status in ("succeeded", "failed"):
            break
        await asyncio.sleep(0.05)

    assert status == "succeeded"
    assert recorded_calls == []


@pytest.mark.anyio
async def test_get_job_not_found_returns_404(authed_client):
    r = await authed_client.client.get(
        "/v1/jobs/job_doesnotexist", headers=authed_client.headers
    )
    assert r.status_code == 404


@pytest.mark.anyio
async def test_get_job_from_other_tenant_returns_404(authed_client):
    other_key = await _seed_key("t_other_jobs", "k_other_jobs", is_admin=False)
    other_headers = {"Authorization": f"Bearer {other_key}"}

    sid = await _create_session(authed_client)
    authed_client.app.state.runtime = FakeAgentRuntime(_events())

    r = await authed_client.client.post(
        f"/v1/sessions/{sid}/messages",
        json={"prompt": "hi", "mode": "async"},
        headers=authed_client.headers,
    )
    job_id = r.json()["job_id"]

    r = await authed_client.client.get(f"/v1/jobs/{job_id}", headers=other_headers)
    assert r.status_code == 404
