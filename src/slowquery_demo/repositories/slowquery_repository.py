"""Data access for the slowquery bookkeeping tables (spec 08).

Module-level async functions following the project's repository pattern
(see ``order_repository.py``). The only layer that imports SQLAlchemy;
the router and schemas never touch it.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.models.slowquery_store import (
    ExplainPlan,
    QueryFingerprint,
    QuerySample,
    Suggestion,
)


async def list_fingerprints(session: AsyncSession) -> list[QueryFingerprint]:
    """All fingerprints, sorted by total_ms descending."""
    stmt = (
        select(QueryFingerprint)
        .order_by(QueryFingerprint.total_ms.desc(), QueryFingerprint.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_fingerprint_by_id(
    session: AsyncSession, fingerprint_id: str
) -> QueryFingerprint | None:
    result = await session.execute(
        select(QueryFingerprint).where(QueryFingerprint.id == fingerprint_id)
    )
    return result.scalar_one_or_none()


async def list_suggestions_for_fingerprint(
    session: AsyncSession, fingerprint_id: str
) -> list[Suggestion]:
    stmt = (
        select(Suggestion)
        .where(Suggestion.fingerprint_id == fingerprint_id)
        .order_by(Suggestion.id)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_explain_plan(
    session: AsyncSession, fingerprint_id: str
) -> ExplainPlan | None:
    result = await session.execute(
        select(ExplainPlan).where(ExplainPlan.fingerprint_id == fingerprint_id)
    )
    return result.scalar_one_or_none()


async def list_recent_samples(
    session: AsyncSession, fingerprint_id: str, *, limit: int = 10
) -> list[QuerySample]:
    stmt = (
        select(QuerySample)
        .where(QuerySample.fingerprint_id == fingerprint_id)
        .order_by(QuerySample.sampled_at.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_all_suggestions(session: AsyncSession) -> dict[str, list[Suggestion]]:
    """All suggestions grouped by fingerprint_id.

    Used by the list endpoint to embed suggestions per fingerprint without
    N+1 queries.
    """
    stmt = select(Suggestion).order_by(Suggestion.fingerprint_id, Suggestion.id)
    result = await session.execute(stmt)
    by_fp: dict[str, list[Suggestion]] = {}
    for s in result.scalars().all():
        by_fp.setdefault(s.fingerprint_id, []).append(s)
    return by_fp
