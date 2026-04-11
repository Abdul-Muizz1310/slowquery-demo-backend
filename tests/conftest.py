"""Shared fixtures for the unit-lane tests.

Integration fixtures (Testcontainers Postgres, dual-branch apps,
seeded datasets) live in ``tests/integration/conftest.py`` and are
added in a later slice when the integration lane is enabled.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from slowquery_demo.core.database import get_db
from slowquery_demo.main import create_app


@pytest.fixture
def empty_session() -> AsyncMock:
    """An ``AsyncSession`` mock whose ``execute`` returns an empty result set.

    This lets API-shape tests exercise 404 paths (repo returns None ->
    service raises NotFoundError -> handler returns 404) without
    spinning up a real database.
    """
    session = AsyncMock()
    result = AsyncMock()
    result.scalar_one_or_none = lambda: None
    result.scalars = lambda: _EmptyScalars()
    session.execute.return_value = result
    return session


class _EmptyScalars:
    def all(self) -> list[object]:
        return []


@pytest.fixture
def test_client(empty_session: AsyncMock) -> Iterator[TestClient]:
    """TestClient against a fresh app with ``get_db`` overridden."""
    app = create_app()

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield empty_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
