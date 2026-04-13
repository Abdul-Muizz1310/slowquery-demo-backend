"""User data access."""

from __future__ import annotations

import uuid
from datetime import datetime

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
        # Keyset pagination: rows strictly before the cursor position.
        # Uses (created_at, id) composite ordering to avoid duplicates.
        from sqlalchemy import and_, or_

        cursor_dt = datetime.fromisoformat(cursor_created_at)
        try:
            cursor_uid = uuid.UUID(cursor_id)
        except ValueError:
            cursor_uid = None

        if cursor_uid is not None:
            stmt = stmt.where(
                or_(
                    User.created_at < cursor_dt,
                    and_(User.created_at == cursor_dt, User.id < cursor_uid),
                )
            )
        else:
            stmt = stmt.where(User.created_at < cursor_dt)
    result = await session.execute(stmt)
    return list(result.scalars().all())
