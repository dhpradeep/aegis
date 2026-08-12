
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey, Session, Usage
from app.schemas.sessions import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDeleteResponse,
    SessionDetail,
    SessionSummary,
    UsageTotals,
)
from app.services.sessions import create_session_record

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])

__all__ = ["router", "owned_session"]


async def owned_session(db: AsyncSession, tenant_id: str, sid: str) -> Session:
    """Fetch a session by id, scoped to tenant_id.

    Raises ApiError.not_found if the session doesn't exist or belongs to a
    different tenant (foreign sessions are indistinguishable from missing
    ones to the caller). Shared with Tasks 12/13.
    """
    row = (
        await db.execute(
            select(Session).where(Session.id == sid, Session.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise ApiError.not_found("Session not found")
    return row


@router.post("", response_model=SessionCreateResponse)
async def create_session(
    body: SessionCreateRequest,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> SessionCreateResponse:
    if body.agent is None:
        raise ApiError.invalid("agent required")

    session = await create_session_record(
        db,
        tenant_id=key.tenant_id,
        agent_id=body.agent,
        title=body.title,
        allow_admin_only=key.is_admin,
    )

    return SessionCreateResponse(
        session_id=session.id,
        profile=session.profile,
        status=session.status,
        created_at=session.created_at,
    )


@router.get("", response_model=list[SessionSummary])
async def list_sessions(
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> list[SessionSummary]:
    rows = (
        (
            await db.execute(
                select(Session)
                .where(Session.tenant_id == key.tenant_id)
                .order_by(Session.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        SessionSummary(
            session_id=s.id,
            profile=s.profile,
            status=s.status,
            title=s.title,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in rows
    ]


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> SessionDetail:
    session = await owned_session(db, key.tenant_id, session_id)

    cost_usd, input_tokens, output_tokens, num_runs = (
        await db.execute(
            select(
                func.coalesce(func.sum(Usage.cost_usd), 0.0),
                func.coalesce(func.sum(Usage.input_tokens), 0),
                func.coalesce(func.sum(Usage.output_tokens), 0),
                func.count(Usage.id),
            ).where(Usage.session_id == session_id)
        )
    ).one()

    return SessionDetail(
        session_id=session.id,
        profile=session.profile,
        agent_id=session.agent_id,
        status=session.status,
        title=session.title,
        created_at=session.created_at,
        updated_at=session.updated_at,
        usage_totals=UsageTotals(
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            num_runs=num_runs,
        ),
    )


@router.delete("/{session_id}", response_model=SessionDeleteResponse)
async def delete_session(
    session_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> SessionDeleteResponse:
    session = await owned_session(db, key.tenant_id, session_id)
    session.status = "ended"
    await db.commit()
    return SessionDeleteResponse(status="ended")
