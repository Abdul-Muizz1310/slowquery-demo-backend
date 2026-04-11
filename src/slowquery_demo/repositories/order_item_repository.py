"""OrderItem data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.models.order_item import OrderItem


async def list_for_order(session: AsyncSession, order_id: uuid.UUID) -> list[OrderItem]:
    # slow-path: seq scan on order_items.order_id.
    stmt = select(OrderItem).where(OrderItem.order_id == order_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_for_product(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    limit: int,
) -> list[OrderItem]:
    # slow-path: seq scan on order_items.product_id.
    stmt = select(OrderItem).where(OrderItem.product_id == product_id).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
