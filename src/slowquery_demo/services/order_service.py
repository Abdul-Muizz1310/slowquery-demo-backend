"""Order + OrderItem business logic."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.errors import OrderNotFoundError
from slowquery_demo.repositories import order_item_repository, order_repository
from slowquery_demo.schemas.order import (
    OrderDTO,
    OrderItemDTO,
    OrderWithItemsDTO,
)
from slowquery_demo.schemas.pagination import PaginatedResponse, clamp_limit


async def get_order_with_items(session: AsyncSession, order_id: uuid.UUID) -> OrderWithItemsDTO:
    order = await order_repository.get_by_id(session, order_id)
    if order is None:
        raise OrderNotFoundError(str(order_id))
    items = await order_item_repository.list_for_order(session, order_id)
    return OrderWithItemsDTO(
        id=order.id,
        user_id=order.user_id,
        status=order.status,  # type: ignore[arg-type]
        total_cents=order.total_cents,
        created_at=order.created_at,
        items=[OrderItemDTO.model_validate(i) for i in items],
    )


async def list_recent_orders(
    session: AsyncSession, *, limit: int | None
) -> PaginatedResponse[OrderDTO]:
    effective_limit = clamp_limit(limit)
    rows = await order_repository.list_recent(session, limit=effective_limit)
    return PaginatedResponse[OrderDTO](
        items=[OrderDTO.model_validate(r) for r in rows],
        next_cursor=None,
    )


async def list_user_orders(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int | None,
) -> PaginatedResponse[OrderDTO]:
    effective_limit = clamp_limit(limit)
    rows = await order_repository.list_for_user(session, user_id, limit=effective_limit)
    return PaginatedResponse[OrderDTO](
        items=[OrderDTO.model_validate(r) for r in rows],
        next_cursor=None,
    )


async def list_items_for_product(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    limit: int | None,
) -> PaginatedResponse[OrderItemDTO]:
    effective_limit = clamp_limit(limit)
    rows = await order_item_repository.list_for_product(session, product_id, limit=effective_limit)
    return PaginatedResponse[OrderItemDTO](
        items=[OrderItemDTO.model_validate(r) for r in rows],
        next_cursor=None,
    )
