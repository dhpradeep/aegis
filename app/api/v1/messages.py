import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.api.v1.sessions import owned_session
from app.core.errors import ApiError
from app.db.models import ApiKey, Event
from app.schemas.messages import EventOut, MessageResult, MessageSendRequest
from app.services import ratelimit
from app.services.agent.session_runner import build_run_config, run_session_message
from app.services.jobs import submit_job
from app.services.ratelimit import check_daily_cost, check_rpm

router = APIRouter(prefix="/v1/sessions/{session_id}/messages", tags=["messages"])


async def _sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    """Format each event dict as an SSE frame: `event: <type>\\ndata: <json>\\n\\n`."""
    async for ev in events:
        yield f"event: {ev['type']}\ndata: {json.dumps(ev)}\n\n"


@router.post("", response_model=None)
async def send_message(
    session_id: str,
    body: MessageSendRequest,
    request: Request,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
):
    session = await owned_session(db, key.tenant_id, session_id)
    if session.status == "running":
        raise ApiError.session_busy()

    session.status = "running"
    await db.commit()

    try:
        await check_rpm(db, key)
        await check_daily_cost(db, key)
    except Exception:
        session.status = "active"
        await db.commit()
        raise

    if body.mode == "async":
        job = await submit_job(request.app, db, session, key, body.prompt)
        return JSONResponse(status_code=202, content={"job_id": job.id, "status": job.status})

    try:
        cfg = await build_run_config(db, key.tenant_id, session, body.prompt)
    except Exception:
        session.status = "active"
        await db.commit()
        raise

    try:
        await ratelimit.run_gate.acquire()
    except Exception:
        session.status = "active"
        await db.commit()
        raise

    runtime = request.app.state.runtime
    events = run_session_message(
        runtime,
        cfg,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        session_id=session_id,
    )

    if body.stream:
        return StreamingResponse(_sse(events), media_type="text/event-stream")

    final: dict | None = None
    async for ev in events:
        if ev["type"] == "result":
            final = ev
        elif ev["type"] == "error" and final is None:
            final = ev

    if final is None or final.get("type") != "result":
        message = (final or {}).get("message", "Agent run failed")
        raise ApiError.agent_error(message)

    return MessageResult(**final)


@router.get("", response_model=list[EventOut])
async def list_messages(
    session_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    await owned_session(db, key.tenant_id, session_id)

    rows = (
        (
            await db.execute(
                select(Event).where(Event.session_id == session_id).order_by(Event.seq)
            )
        )
        .scalars()
        .all()
    )
    return [
        EventOut(
            seq=r.seq,
            type=r.type,
            payload=json.loads(r.payload_json),
            created_at=r.created_at,
        )
        for r in rows
    ]
