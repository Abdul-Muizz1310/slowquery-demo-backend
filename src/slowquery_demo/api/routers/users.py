"""/users routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.database import get_db
from slowquery_demo.schemas.order import OrderDTO
from slowquery_demo.schemas.pagination import PaginatedResponse, decode_cursor
from slowquery_demo.schemas.user import UserDTO
from slowquery_demo.services import order_service, user_service

router = APIRouter(prefix="/users", tags=["users"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
Limit = Annotated[int | None, Query()]
CursorParam = Annotated[str | None, Query()]


@router.get("", response_model=PaginatedResponse[UserDTO])
async def list_users(
    session: DbSession,
    limit: Limit = None,
    cursor: CursorParam = None,
) -> PaginatedResponse[UserDTO]:
    created_at: str | None = None
    id_: str | None = None
    if cursor is not None:
        parsed = decode_cursor(cursor)
        created_at = parsed.created_at
        id_ = parsed.id
    return await user_service.list_users(
        session, limit=limit, cursor_created_at=created_at, cursor_id=id_
    )


@router.get("/{user_id}", response_model=UserDTO)
async def get_user(user_id: uuid.UUID, session: DbSession) -> UserDTO:
    return await user_service.get_user(session, user_id)


@router.get("/{user_id}/orders", response_model=PaginatedResponse[OrderDTO])
async def list_user_orders(
    user_id: uuid.UUID,
    session: DbSession,
    limit: Limit = None,
) -> PaginatedResponse[OrderDTO]:
    return await order_service.list_user_orders(session, user_id, limit=limit)
