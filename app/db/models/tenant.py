from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column()
    # Default model (alias like "sonnet" or a full id like "claude-sonnet-5")
    # used by the OpenAI-compat chat endpoint when the request omits `model`.
    # None falls back to the global Settings.default_model.
    default_model: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
