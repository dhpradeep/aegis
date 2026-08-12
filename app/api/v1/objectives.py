from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey, Event, Objective
from app.schemas.objectives import (
    ObjectiveCreateRequest,
    ObjectiveDetail,
    ObjectiveSubmitted,
    ObjectiveSummary,
)
from app.services.objectives import submit_objective

router = APIRouter(prefix="/v1/objectives", tags=["objectives"])

__all__ = ["router"]


async def _owned_objective(db: AsyncSession, tenant_id: str, objective_id: str) -> Objective:
    """Fetch an Objective by id, scoped to tenant_id.

    Raises ApiError.not_found if it doesn't exist or belongs to a different
    tenant (foreign objectives are indistinguishable from missing ones to
    the caller), mirroring `app.api.v1.sessions.owned_session`.
    """
    row = (
        await db.execute(
            select(Objective).where(
                Objective.id == objective_id, Objective.tenant_id == tenant_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError.not_found("Objective not found")
    return row


def _summary(obj: Objective) -> ObjectiveSummary:
    return ObjectiveSummary(
        objective_id=obj.id,
        agent_id=obj.agent_id,
        goal=obj.goal,
        status=obj.status,
        iterations_done=obj.iterations_done,
        cost_usd=obj.cost_usd,
        created_at=obj.created_at,
        finished_at=obj.finished_at,
    )


def _detail(obj: Objective) -> ObjectiveDetail:
    return ObjectiveDetail(
        objective_id=obj.id,
        agent_id=obj.agent_id,
        goal=obj.goal,
        rubric=obj.rubric,
        status=obj.status,
        max_cost_usd=obj.max_cost_usd,
        max_iterations=obj.max_iterations,
        iterations_done=obj.iterations_done,
        cost_usd=obj.cost_usd,
        result_text=obj.result_text,
        session_id=obj.session_id,
        created_at=obj.created_at,
        finished_at=obj.finished_at,
    )


@router.post("", response_model=None, status_code=202)
async def create_objective(
    body: ObjectiveCreateRequest,
    request: Request,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> ObjectiveSubmitted:
    obj = await submit_objective(
        request.app,
        db,
        tenant_id=key.tenant_id,
        api_key_id=key.id,
        agent_id=body.agent,
        goal=body.goal,
        rubric=body.rubric,
        max_cost_usd=body.max_cost_usd,
        max_iterations=body.max_iterations,
        is_admin=key.is_admin,
    )

    return ObjectiveSubmitted(objective_id=obj.id, status=obj.status)


@router.get("", response_model=list[ObjectiveSummary])
async def list_objectives(
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> list[ObjectiveSummary]:
    rows = (
        (
            await db.execute(
                select(Objective)
                .where(Objective.tenant_id == key.tenant_id)
                .order_by(Objective.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_summary(o) for o in rows]


@router.get("/{objective_id}", response_model=ObjectiveDetail)
async def get_objective(
    objective_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> ObjectiveDetail:
    obj = await _owned_objective(db, key.tenant_id, objective_id)
    return _detail(obj)


async def _sse(events: list[Event]) -> AsyncIterator[str]:
    """Format each stored Event row as an SSE frame: `event: <type>\\ndata: <json>\\n\\n`.

    This is a one-shot replay of the objective's working-session events as
    they stand right now (no live tailing) — good enough until a follow-up
    adds streaming of in-progress runs.
    """
    for ev in events:
        yield f"event: {ev.type}\ndata: {ev.payload_json}\n\n"


@router.get("/{objective_id}/events")
async def stream_objective_events(
    objective_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    obj = await _owned_objective(db, key.tenant_id, objective_id)

    events: list[Event] = []
    if obj.session_id is not None:
        events = (
            (
                await db.execute(
                    select(Event)
                    .where(Event.session_id == obj.session_id)
                    .order_by(Event.seq)
                )
            )
            .scalars()
            .all()
        )

    return StreamingResponse(_sse(events), media_type="text/event-stream")
