"""Async job submission + background execution + webhook delivery (Task 20).

`submit_job` creates a queued `Job` row and schedules `run_job` as a
background `asyncio.Task`, returning immediately so the caller (the
`POST /messages {"mode":"async"}` route) can respond `202` without waiting
for the agent run.

`run_job` reuses Task 12's `build_run_config` / `run_session_message` from
`app.services.agent.session_runner` for the actual run + event/usage
persistence, so the async path stays byte-for-byte identical to the sync
path in how it drives the runtime and writes `Event`/`Usage` rows. It opens
its own `SessionLocal()` throughout — never the request-scoped session that
handed off to it — since it keeps running long after the original request
has completed.

`fire_webhook` is best-effort: delivery errors (including "no webhook
configured for this tenant") are swallowed and logged, never raised, and
there are no retries in v1.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from secrets import token_hex
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import SessionLocal
from app.db.models import ApiKey, Job, Session, WebhookConfig
from app.db.models._base import _utcnow
from app.services import ratelimit
from app.services.agent.session_runner import build_run_config, run_session_message

logger = logging.getLogger("app.jobs")

__all__ = ["submit_job", "run_job", "fire_webhook"]

# Keep strong references to scheduled background tasks so they aren't
# garbage-collected mid-run (a well-known asyncio.create_task gotcha).
_background_tasks: set[asyncio.Task] = set()


async def submit_job(
    app: Any, db: AsyncSession, session: Session, key: ApiKey, prompt: str
) -> Job:
    """Create a queued `Job` and schedule its background run.

    Returns the `Job` row immediately; the agent run and webhook delivery
    happen later in `run_job`, scheduled via `asyncio.create_task`.
    """
    job = Job(
        id="job_" + token_hex(8),
        tenant_id=key.tenant_id,
        session_id=session.id,
        status="queued",
        prompt=prompt,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    task = asyncio.create_task(run_job(app, job.id, api_key_id=key.id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return job


def _payload(job: Job) -> dict:
    return {
        "job_id": job.id,
        "session_id": job.session_id,
        "status": job.status,
        "result": json.loads(job.result_json) if job.result_json else None,
    }


async def _fail(db: AsyncSession, job: Job, session: Session | None, error: str) -> None:
    job.status = "failed"
    job.error = error
    job.finished_at = _utcnow()
    if session is not None:
        session.status = "active"
    await db.commit()
    await fire_webhook(db, job.tenant_id, _payload(job))


async def run_job(app: Any, job_id: str, *, api_key_id: str) -> None:
    """Run the agent for `job_id` in the background, against its own DB session.

    Builds the same `RunConfig` as the sync route (`build_run_config`), then
    drives `run_session_message` — which persists `Event`/`Usage` rows and
    captures `sdk_session_id` exactly as Task 12's sync path does. Sets the
    job's terminal status + `result_json`/`error` + `finished_at`, then fires
    the tenant's webhook (if configured) with the outcome.
    """
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return

        session = await db.get(Session, job.session_id)
        if session is None:
            await _fail(db, job, None, "Session not found")
            return

        job.status = "running"
        await db.commit()

        try:
            cfg = await build_run_config(db, job.tenant_id, session, job.prompt)
        except Exception as exc:
            await _fail(db, job, session, str(exc))
            return

        try:
            await ratelimit.run_gate.acquire()
        except Exception as exc:
            await _fail(db, job, session, str(exc))
            return

    # run_gate is released inside run_session_message's `finally`, mirroring
    # the sync route.
    runtime = app.state.runtime
    events = run_session_message(
        runtime,
        cfg,
        tenant_id=job.tenant_id,
        api_key_id=api_key_id,
        session_id=job.session_id,
    )

    final: dict | None = None
    async for ev in events:
        if ev["type"] == "result":
            final = ev
        elif ev["type"] == "error" and final is None:
            final = ev

    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
        if final is not None and final.get("type") == "result":
            job.status = "succeeded"
            job.result_json = json.dumps(final)
        else:
            job.status = "failed"
            job.error = (final or {}).get("message", "Agent run failed")
        job.finished_at = _utcnow()
        await db.commit()

        await fire_webhook(db, job.tenant_id, _payload(job))


async def fire_webhook(db: AsyncSession, tenant_id: str, payload: dict) -> None:
    """Best-effort webhook delivery for `tenant_id`'s configured `WebhookConfig`.

    No-ops if no webhook is configured. Signs the exact JSON bytes sent (not
    a re-serialization of `payload`) as `X-Signature: sha256=<hex hmac>` so
    receivers can verify against the raw request body. Swallows and logs any
    delivery error — v1 does no retries.
    """
    config = await db.get(WebhookConfig, tenant_id)
    if config is None:
        return

    body = json.dumps(payload).encode()
    signature = hmac.new(config.secret.encode(), body, hashlib.sha256).hexdigest()

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                config.url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Signature": f"sha256={signature}",
                },
            )
    except Exception:
        logger.exception("webhook delivery failed for tenant %s", tenant_id)
