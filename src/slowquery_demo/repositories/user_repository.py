"""User data access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.models.user import User


async def get_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def list_users(
    session: AsyncSession,
    *,
    limit: int,
    cursor_created_at: str | None = None,
    cursor_id: str | None = None,
) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc(), User.id.desc()).limit(limit)
    if cursor_created_at is not None and cursor_id is not None:
        # Keyset pagination: rows strictly older than the cursor.
        stmt = stmt.where(User.created_at <= cursor_created_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())
