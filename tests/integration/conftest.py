"""Integration test fixtures (Testcontainers Postgres).

Everything under ``tests/integration/`` is gated by
``@pytest.mark.integration`` and filtered out of the default CI run.
To run locally, start Docker Desktop and:

    uv run pytest -m integration

A session-scoped Postgres container boots once per run; each
integration test gets a clean schema via ``alembic upgrade head``
against that container. Subsequent fixtures layer on (seeded data,
full-app test clients) as the integration lane fills out.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer


def _asyncpg_url(sync_url: str) -> str:
    """Normalise a Testcontainers Postgres URL to the asyncpg dialect."""
    if sync_url.startswith("postgresql+psycopg2://"):
        return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql+asyncpg://"):
        return sync_url
    raise RuntimeError(f"unexpected Postgres URL shape: {sync_url!r}")


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    """One Postgres container per session.

    Imported lazily so the unit lane doesn't pay for testcontainers at
    collection time. Raises pytest.skip if Docker isn't reachable —
    that's the normal state on machines without Docker Desktop running.
    """
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - dev-only import
        pytest.skip("testcontainers[postgres] not installed")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:  # docker unreachable
        pytest.skip(f"docker unreachable for Testcontainers: {exc}")

    try:
        yield container
    finally:
        container.stop()


@pytest.fixture
async def pg_engine(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Async engine pointed at a freshly-migrated Postgres.

    Each test gets a clean schema: the fixture runs ``alembic
    downgrade base`` followed by ``alembic upgrade head`` (via a
    subprocess so the async/sync alembic event-loop mismatch
    doesn't bite).
    """
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)

    # Alembic env.py reads DATABASE_URL from os.environ.
    monkeypatch.setenv("DATABASE_URL", async_url)

    # Alembic via subprocess keeps its own event loop isolated from
    # pytest-asyncio's loop.
    env = {**os.environ, "DATABASE_URL": async_url}
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        check=False,
        env=env,
        capture_output=True,
    )
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env=env,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed: {result.stderr.decode()}")

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()
