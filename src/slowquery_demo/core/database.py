"""Async SQLAlchemy engine + session factory.

Kept intentionally tiny: one engine, one session factory, and a
``get_db()`` FastAPI dependency that yields an ``AsyncSession`` per
request from a factory stored on ``app.state.db_sessionmaker`` by
``create_app()``.

Unit tests override ``get_db`` via ``app.dependency_overrides`` so the
real session factory is never reached under ``pytest -m "not
integration"``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from slowquery_demo.core.db_config import normalise_asyncpg_url


def build_engine(url: str) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build an async engine + session factory against ``url``.

    The incoming URL is normalised via :func:`normalise_asyncpg_url` so
    callers can pass a raw Neon libpq connection string
    (``postgresql://.../?sslmode=require&channel_binding=require``) and
    still get a working engine.
    """
    resolved = normalise_asyncpg_url(url)
    engine = create_async_engine(resolved, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, factory


async def get_db(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an ``AsyncSession`` per request.

    Reads the session factory from ``request.app.state.db_sessionmaker``,
    which is populated by :func:`slowquery_demo.main.create_app`. Unit
    tests override this dependency with a mock session, so the real
    path is only exercised under ``pytest -m integration`` or at
    runtime.
    """
    factory: async_sessionmaker[AsyncSession] = request.app.state.db_sessionmaker
    async with factory() as session:
        yield session
