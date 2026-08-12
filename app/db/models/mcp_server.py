from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class McpServer(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    # NULL tenant_id = global (available to all tenants).
    tenant_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("tenants.id"), nullable=True, default=None
    )
    name: Mapped[str] = mapped_column()
    kind: Mapped[str] = mapped_column()  # "http" | "sse" | "stdio"
    url: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    headers_json: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    command: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    args_json: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
