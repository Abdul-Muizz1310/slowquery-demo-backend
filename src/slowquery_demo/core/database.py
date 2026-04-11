"""Async SQLAlchemy engine + session factory.

Kept intentionally tiny: one engine, one session factory, and a
``get_db()`` FastAPI dependency that yields an ``AsyncSession`` per
request. Tests swap the dependency via ``app.dependency_overrides``
so no real database contact is needed to exercise API shape.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from slowquery_demo.core.db_config import get_database_url, normalise_asyncpg_url


def build_engine(url: str | None = None) -> tuple[object, async_sessionmaker[AsyncSession]]:
    """Build an async engine + session factory against ``url``.

    The incoming URL is normalised via :func:`normalise_asyncpg_url` so
    Settings-driven callers (e.g. ``create_app()``) can pass a raw Neon
    libpq connection string and still get a working engine.
    """
    resolved = normalise_asyncpg_url(url) if url else get_database_url()
    engine = create_async_engine(resolved, future=True)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields an ``AsyncSession`` per request.

    The real engine is built lazily in ``create_app()``; this default
    generator is the placeholder used before the override is installed.
    Unit tests override this dependency with a mock session, so the
    real ``get_db`` never runs under ``pytest -m "not integration"``.
    """
    raise RuntimeError(
        "get_db() was called without an override; install_database(app) must run first"
    )
    yield  # pragma: no cover - unreachable, keeps mypy happy on the generator type
