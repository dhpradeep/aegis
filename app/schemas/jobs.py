from datetime import datetime
from typing import Any

from pydantic import BaseModel


class JobSubmitted(BaseModel):
    """Returned with 202 from `POST /messages {"mode":"async"}`."""

    job_id: str
    status: str


class JobOut(BaseModel):
    id: str
    status: str
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    finished_at: datetime | None = None
