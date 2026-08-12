from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BillingConfig(Base):
    __tablename__ = "billing_configs"

    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    price_per_mtok_input: Mapped[float] = mapped_column()
    price_per_mtok_output: Mapped[float] = mapped_column()
    markup: Mapped[float] = mapped_column()
