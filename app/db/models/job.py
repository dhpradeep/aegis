from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Job(Base):
    """A background agent run submitted via `POST /messages {"mode":"async"}`.

    `status` moves queued -> running -> succeeded|failed. `result_json` holds
    the terminal `result` event (JSON-encoded) on success; `error` holds a
    message on failure. See app/services/jobs.py.
    """

    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"))
    status: Mapped[str] = mapped_column()  # "queued" | "running" | "succeeded" | "failed"
    prompt: Mapped[str] = mapped_column()
    result_json: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    error: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
