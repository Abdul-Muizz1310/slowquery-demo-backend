"""/orders routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.database import get_db
from slowquery_demo.schemas.order import OrderDTO, OrderWithItemsDTO
from slowquery_demo.schemas.pagination import PaginatedResponse
from slowquery_demo.services import order_service

router = APIRouter(prefix="/orders", tags=["orders"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
Limit = Annotated[int | None, Query()]


@router.get("", response_model=PaginatedResponse[OrderDTO])
async def list_recent_orders(
    session: DbSession, limit: Limit = None
) -> PaginatedResponse[OrderDTO]:
    return await order_service.list_recent_orders(session, limit=limit)


@router.get("/{order_id}", response_model=OrderWithItemsDTO)
async def get_order_with_items(order_id: uuid.UUID, session: DbSession) -> OrderWithItemsDTO:
    return await order_service.get_order_with_items(session, order_id)
