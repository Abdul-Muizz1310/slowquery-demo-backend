"""/order_items routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.database import get_db
from slowquery_demo.schemas.order import OrderItemDTO
from slowquery_demo.schemas.pagination import PaginatedResponse
from slowquery_demo.services import order_service

router = APIRouter(prefix="/order_items", tags=["order_items"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
ProductIdParam = Annotated[uuid.UUID, Query()]
Limit = Annotated[int | None, Query()]


@router.get("", response_model=PaginatedResponse[OrderItemDTO])
async def list_items_for_product(
    product_id: ProductIdParam,
    session: DbSession,
    limit: Limit = None,
) -> PaginatedResponse[OrderItemDTO]:
    return await order_service.list_items_for_product(session, product_id, limit=limit)
