from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class CompletionLog(Base):
    """One stored record per OpenAI-compatible /v1/chat/completions call.

    The chat shim is stateless (no session), so this table is the durable
    record of each individual completion: the request messages, the response
    text, the resolved model, and usage/cost — reviewable from the DB and the
    admin dashboard.
    """

    __tablename__ = "completion_logs"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    api_key_id: Mapped[Optional[str]] = mapped_column(ForeignKey("api_keys.id"), nullable=True)
    model: Mapped[str] = mapped_column()
    streamed: Mapped[bool] = mapped_column(default=False)
    # JSON-encoded request messages (the OpenAI `messages` array).
    request_json: Mapped[str] = mapped_column()
    response_text: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
