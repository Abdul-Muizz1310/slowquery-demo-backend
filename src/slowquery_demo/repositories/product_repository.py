"""Product data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.models.product import Product


async def get_by_id(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    result = await session.execute(select(Product).where(Product.id == product_id))
    return result.scalar_one_or_none()


async def list_products(session: AsyncSession, *, limit: int) -> list[Product]:
    stmt = select(Product).order_by(Product.created_at.desc(), Product.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
