"""Async SQLAlchemy engines that follow the active Neon branch (spec 15).

slowquery-detective binds its hooks and its EXPLAIN worker to the engine
handed to ``install`` for the app's whole lifetime, so a branch switch must
not replace that engine. Instead every engine here connects through an
``async_creator`` that dials :class:`BranchTarget`'s current URL: a switch
health-checks the target (:func:`check_branch_reachable`), sets the URL and
disposes the pools, and every new connection then goes to the target branch.

Two engines share one target:

* the **commerce engine** serves requests and is the one slowquery-detective
  observes;
* the **health engine** serves only ``/health``'s ``SELECT 1`` and is never
  instrumented, so the probe can never become a fingerprint.

``get_db()`` yields an ``AsyncSession`` per request from the factory on
``app.state.db_sessionmaker``; unit tests override it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Final

import asyncpg
from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

_CONNECT_TIMEOUT_S: Final = 10.0
_HEALTH_CHECK_TIMEOUT_S: Final = 5.0


class BranchTarget:
    """The URL of the branch every new connection goes to. One per app."""

    def __init__(self, url: str) -> None:
        if not url:
            raise ValueError("a branch target needs a database URL")
        self.url = url


def _connector(target: BranchTarget) -> Any:
    async def _connect() -> Any:
        return await asyncpg.connect(dsn=to_raw_asyncpg_dsn(target.url), timeout=_CONNECT_TIMEOUT_S)

    return _connect


def build_branch_engine(
    target: BranchTarget, *, pool_size: int = 5, max_overflow: int = 10
) -> AsyncEngine:
    """An engine whose connections go to ``target.url`` at connect time.

    Neon suspends idle compute and Render free tier idles too, so a pooled
    connection can be dropped between requests: pre-ping discards dead ones
    and recycle caps their age (P9). Explicit pool limits keep a handful of
    dashboard tabs from exhausting the pool.
    """
    return create_async_engine(
        "postgresql+asyncpg://",
        async_creator=_connector(target),
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=10,
    )


def build_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def check_branch_reachable(url: str) -> None:
    """Raise unless ``url`` accepts a connection and answers ``SELECT 1`` in time."""
    conn = await asyncpg.connect(dsn=to_raw_asyncpg_dsn(url), timeout=_HEALTH_CHECK_TIMEOUT_S)
    try:
        await asyncio.wait_for(conn.fetchval("SELECT 1"), timeout=_HEALTH_CHECK_TIMEOUT_S)
    finally:
        await conn.close()


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an ``AsyncSession`` per request."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with factory() as session:
        yield session
