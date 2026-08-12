from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateBucket(Base):
    __tablename__ = "rate_buckets"
    __table_args__ = (
        UniqueConstraint("api_key_id", "window", name="uq_rate_buckets_api_key_id_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    api_key_id: Mapped[str] = mapped_column(ForeignKey("api_keys.id"))
    window: Mapped[str] = mapped_column()
    count: Mapped[int] = mapped_column()
    cost_usd: Mapped[float] = mapped_column()
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
