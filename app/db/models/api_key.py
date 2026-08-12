from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    key_hash: Mapped[str] = mapped_column(unique=True)
    prefix: Mapped[str] = mapped_column()
    name: Mapped[str] = mapped_column()
    # None = unlimited (no requests-per-minute cap).
    rpm: Mapped[Optional[int]] = mapped_column(nullable=True, default=None)
    # None = unlimited (no daily cost cap).
    daily_cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True, default=None)
    is_admin: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
