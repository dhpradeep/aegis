from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Objective(Base):
    """A goal-driven autonomous run (id format: `"obj_" + hex`).

    Drives a working `Session` toward `goal`, judged against `rubric`, for up
    to `max_iterations` loop iterations or until `max_cost_usd` is spent.
    `status` moves running -> succeeded|failed|budget_exhausted|max_iterations.
    Loop events (`plan`/`text`/`tool_use`/`result` from the driven session, and
    the `objective.iteration_started`/`objective.evaluation`/`objective.finished`
    markers) are persisted as `Event` rows on `session_id` — no separate table.
    """

    __tablename__ = "objectives"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    api_key_id: Mapped[Optional[str]] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"))
    goal: Mapped[str] = mapped_column()
    rubric: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(default="queued")
    max_cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True, default=None)
    max_iterations: Mapped[int] = mapped_column()
    iterations_done: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    result_text: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
