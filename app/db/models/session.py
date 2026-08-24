from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    profile: Mapped[str] = mapped_column()
    agent_id: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    overrides_json: Mapped[str] = mapped_column()
    mcp_names_json: Mapped[str] = mapped_column()
    workspace_path: Mapped[str] = mapped_column()
    sdk_session_id: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    status: Mapped[str] = mapped_column()  # "active" | "running" | "ended"
    title: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    # CLI conversations routed through the compat shims: hash + length of the
    # client transcript last seen, used to recognize the next turn.
    conv_hash: Mapped[Optional[str]] = mapped_column(nullable=True, default=None, index=True)
    conv_turns: Mapped[int] = mapped_column(default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
