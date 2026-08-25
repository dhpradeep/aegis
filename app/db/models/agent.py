from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models._base import _utcnow


class Agent(Base):
    """A reusable, admin-defined agent config (id format: `"agt_" + hex`)."""

    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)
    description: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    model: Mapped[str] = mapped_column()
    effort: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    system_prompt: Mapped[Optional[str]] = mapped_column(nullable=True, default=None)
    allowed_tools_json: Mapped[str] = mapped_column()
    permission_mode: Mapped[str] = mapped_column()
    mcp_names_json: Mapped[str] = mapped_column()
    roster_json: Mapped[str] = mapped_column()
    max_cost_usd: Mapped[Optional[float]] = mapped_column(nullable=True, default=None)
    max_iterations: Mapped[int] = mapped_column(default=6)
    is_admin_only: Mapped[bool] = mapped_column(default=False)
    portal_visible: Mapped[bool] = mapped_column(default=False, server_default="0")
    # When True, runs use the SDK's `bypassPermissions` mode: the agent may
    # execute any command/tool with no approval gate. Meant for trusted,
    # admin-only "builder" agents in an isolated workspace. Overrides
    # `permission_mode` for the run when set.
    bypass_permissions: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
