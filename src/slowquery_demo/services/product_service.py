"""Product business logic."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.errors import ProductNotFoundError
from slowquery_demo.repositories import product_repository
from slowquery_demo.schemas.pagination import PaginatedResponse, clamp_limit
from slowquery_demo.schemas.product import ProductDTO


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> ProductDTO:
    product = await product_repository.get_by_id(session, product_id)
    if product is None:
        raise ProductNotFoundError(str(product_id))
    return ProductDTO.model_validate(product)


async def list_products(
    session: AsyncSession, *, limit: int | None
) -> PaginatedResponse[ProductDTO]:
    effective_limit = clamp_limit(limit)
    rows = await product_repository.list_products(session, limit=effective_limit)
    dtos = [ProductDTO.model_validate(r) for r in rows]
    return PaginatedResponse[ProductDTO](items=dtos, next_cursor=None)
