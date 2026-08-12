from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey
from app.services import claude_usage
from app.services.billing import usage_summary

router = APIRouter(prefix="/v1/usage", tags=["usage"])

__all__ = ["router"]


def _parse_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ApiError.invalid(f"Invalid date: {value}")


@router.get("")
async def get_usage(
    from_: str | None = Query(default=None, alias="from"),
    to: str | None = Query(default=None),
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> dict:
    dt_from = _parse_date(from_)
    dt_to = _parse_date(to)
    return await usage_summary(db, key.tenant_id, dt_from=dt_from, dt_to=dt_to)


@router.get("/plan")
async def get_plan_usage(key: ApiKey = Depends(require_key)) -> dict:
    """Live Claude *subscription* quota — the session (5-hour) and weekly limits
    Claude meters, as utilization percentages with reset timestamps. This is the
    plan quota shared by the whole deployment, distinct from the per-tenant token
    usage at ``GET /v1/usage``. Returns ``{"available": false}`` when the runtime
    isn't signed in to a Claude subscription."""
    return await claude_usage.get_plan_usage()
