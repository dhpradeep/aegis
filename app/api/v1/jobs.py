import json

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_key
from app.core.errors import ApiError
from app.db.models import ApiKey, Job
from app.schemas.jobs import JobOut

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

__all__ = ["router"]


@router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    key: ApiKey = Depends(require_key),
    db: AsyncSession = Depends(get_db),
) -> JobOut:
    job = await db.get(Job, job_id)
    if job is None or job.tenant_id != key.tenant_id:
        raise ApiError.not_found("Job not found")

    return JobOut(
        id=job.id,
        status=job.status,
        result=json.loads(job.result_json) if job.result_json else None,
        error=job.error,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )
