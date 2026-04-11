"""Order data access — the slow endpoints live here."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.models.order import Order


async def get_by_id(session: AsyncSession, order_id: uuid.UUID) -> Order | None:
    result = await session.execute(select(Order).where(Order.id == order_id))
    return result.scalar_one_or_none()


async def list_recent(session: AsyncSession, *, limit: int) -> list[Order]:
    # slow-path: ORDER BY created_at DESC without a composite index.
    stmt = select(Order).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_for_user(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
) -> list[Order]:
    # slow-path: seq scan on orders.user_id (no index on slow branch).
    stmt = (
        select(Order)
        .where(Order.user_id == user_id)
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())
