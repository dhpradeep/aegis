from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Usage(Base):
    __tablename__ = "usage"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    api_key_id: Mapped[Optional[str]] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    # Nullable: a later OpenAI-compatibility task creates Usage rows with no session.
    session_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("sessions.id"), nullable=True, default=None
    )
    input_tokens: Mapped[int] = mapped_column()
    output_tokens: Mapped[int] = mapped_column()
    cache_read_tokens: Mapped[int] = mapped_column()
    cost_usd: Mapped[float] = mapped_column()
    duration_ms: Mapped[int] = mapped_column()
    num_turns: Mapped[int] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
