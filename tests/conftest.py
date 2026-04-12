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


@pytest.fixture
def fake_locust_stats_high_p95():  # type: ignore[no-untyped-def]
    """Stats payload whose p95 exceeds the traffic generator's threshold."""
    from scripts.traffic_generator import TrafficStats

    return TrafficStats(total=100, failures=0, p95_ms=40_000.0)


@pytest.fixture
def fake_locust_stats_high_failures():  # type: ignore[no-untyped-def]
    """Stats payload whose failure rate exceeds the traffic generator's threshold."""
    from scripts.traffic_generator import TrafficStats

    return TrafficStats(total=100, failures=50, p95_ms=10.0)


@pytest.fixture(autouse=True)
def _isolated_runtime(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Isolate each test from on-disk state that would leak across tests.

    Two redirections per test:

    1. ``BRANCH_STATE_FILE`` points at ``tmp_path/.branch_state`` so the
       branch switcher's persistence file doesn't accumulate across
       tests and make ordering relevant.
    2. ``cwd`` is set to ``tmp_path`` so :class:`Settings`' default
       ``env_file=".env"`` looks at the tmp dir instead of the real
       repo-root ``.env``. Tests that monkeypatch individual env vars
       need to see those changes without the real ``.env`` adding
       its own values back in.
    """
    state_file = tmp_path / ".branch_state"
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state_file))
    monkeypatch.chdir(tmp_path)
    yield


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
    from slowquery_demo.core.database import get_db
    from slowquery_demo.main import create_app

    app = create_app()

    # Disable the real engine-builder so branch-switch tests don't
    # try to connect to Neon. Unit tests only exercise the state
    # transition + timing envelope; real engine rebuilds are
    # integration-lane (spec 10).
    switcher = getattr(app.state, "branch_switcher", None)
    if switcher is not None:
        switcher._engine_builder = None  # type: ignore[attr-defined]

    async def _override() -> AsyncGenerator[AsyncMock, None]:
        yield empty_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
