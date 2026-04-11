"""User business logic."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.errors import UserNotFoundError
from slowquery_demo.repositories import user_repository
from slowquery_demo.schemas.pagination import PaginatedResponse, clamp_limit, encode_cursor
from slowquery_demo.schemas.user import UserDTO


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> UserDTO:
    user = await user_repository.get_by_id(session, user_id)
    if user is None:
        raise UserNotFoundError(str(user_id))
    return UserDTO.model_validate(user)


async def list_users(
    session: AsyncSession,
    *,
    limit: int | None,
    cursor_created_at: str | None,
    cursor_id: str | None,
) -> PaginatedResponse[UserDTO]:
    effective_limit = clamp_limit(limit)
    rows = await user_repository.list_users(
        session,
        limit=effective_limit,
        cursor_created_at=cursor_created_at,
        cursor_id=cursor_id,
    )
    dtos = [UserDTO.model_validate(r) for r in rows]
    next_cursor = (
        encode_cursor(str(rows[-1].created_at), str(rows[-1].id))
        if len(rows) == effective_limit
        else None
    )
    return PaginatedResponse[UserDTO](items=dtos, next_cursor=next_cursor)
