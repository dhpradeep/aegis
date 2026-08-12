from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WebhookConfig(Base):
    """Per-tenant webhook delivery target for job-completion notifications.

    One row per tenant (tenant_id is the primary key). See
    app/services/jobs.py:fire_webhook for how `secret` is used to sign
    delivered payloads.
    """

    __tablename__ = "webhook_configs"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    url: Mapped[str] = mapped_column()
    secret: Mapped[str] = mapped_column()
