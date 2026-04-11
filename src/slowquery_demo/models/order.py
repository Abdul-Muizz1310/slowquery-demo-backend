"""Order ORM model.

The ``order_status`` Postgres ENUM is declared here and shared with the
migration. ``create_type=False`` in the column keeps alembic in charge of
creating/dropping the type so the migration stays the single source of
truth.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import ENUM, UUID
from sqlalchemy.orm import Mapped, mapped_column

from slowquery_demo.models.base import Base

ORDER_STATUS_VALUES = ("pending", "paid", "shipped", "cancelled")

order_status_enum = ENUM(
    *ORDER_STATUS_VALUES,
    name="order_status",
    create_type=False,
)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (CheckConstraint("total_cents >= 0", name="ck_orders_total_cents_nonneg"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(order_status_enum, nullable=False, server_default="pending")
    total_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
