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
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

# Project root (two parents up from tests/integration/conftest.py).
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])

# Seed sizes used by fixtures — small for speed.
_SEED_USERS = 100
_SEED_PRODUCTS = 20
_SEED_ORDERS = 1000
_SEED_ORDER_ITEMS = 5000
_SEED_SEED = 42


# ---------------------------------------------------------------------------
# Auto-skip when Docker is unavailable
# ---------------------------------------------------------------------------


def _docker_is_available() -> bool:
    """Return True if Docker daemon is reachable."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Skip all integration tests when Docker is unreachable."""
    if not items:
        return

    if _docker_is_available():
        return

    skip_marker = pytest.mark.skip(
        reason="Docker daemon not available (integration tests require Testcontainers)"
    )
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip_marker)


def _asyncpg_url(sync_url: str) -> str:
    """Normalise a Testcontainers Postgres URL to the asyncpg dialect."""
    if sync_url.startswith("postgresql+psycopg2://"):
        return sync_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql://"):
        return sync_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if sync_url.startswith("postgresql+asyncpg://"):
        return sync_url
    raise RuntimeError(f"unexpected Postgres URL shape: {sync_url!r}")


def _run_alembic(
    cmd: list[str], env: dict[str, str], *, check: bool = True
) -> subprocess.CompletedProcess[bytes]:
    """Run an alembic command as a subprocess from the project root."""
    return subprocess.run(
        [sys.executable, "-m", "alembic", *cmd],
        check=check,
        env=env,
        capture_output=True,
        cwd=_PROJECT_ROOT,
    )


def _migrate_fresh(async_url: str) -> None:
    """Downgrade base then upgrade head for a clean schema."""
    env = {**os.environ, "DATABASE_URL": async_url}
    _run_alembic(["downgrade", "base"], env, check=False)
    result = _run_alembic(["upgrade", "head"], env)
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade failed: {result.stderr.decode()}")


def _seed_slow(async_url: str) -> None:
    """Run seed_slow.py against the given URL."""
    env = {**os.environ, "DATABASE_URL": async_url}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from scripts.seed_slow import main; "
            f"asyncio.run(main(['--reset', '--users', '{_SEED_USERS}', "
            f"'--orders', '{_SEED_ORDERS}', '--order-items', '{_SEED_ORDER_ITEMS}', "
            f"'--products', '{_SEED_PRODUCTS}']))",
        ],
        env=env,
        capture_output=True,
        cwd=_PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"seed_slow failed: {result.stderr.decode()}")


def _seed_fast(async_url: str) -> None:
    """Run seed_fast.py against the given URL (includes indexes)."""
    env = {**os.environ, "DATABASE_URL_FAST": async_url}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio; from scripts.seed_fast import main; "
            f"asyncio.run(main(['--reset', '--users', '{_SEED_USERS}', "
            f"'--orders', '{_SEED_ORDERS}', '--order-items', '{_SEED_ORDER_ITEMS}', "
            f"'--products', '{_SEED_PRODUCTS}']))",
        ],
        env=env,
        capture_output=True,
        cwd=_PROJECT_ROOT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"seed_fast failed: {result.stderr.decode()}")


# ---------------------------------------------------------------------------
# Session-scoped container
# ---------------------------------------------------------------------------


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


@pytest.fixture(scope="session")
def pg_container_fast() -> Iterator[PostgresContainer]:
    """Second Postgres container for the fast branch (session-scoped)."""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers[postgres] not installed")

    try:
        container = PostgresContainer("postgres:16-alpine")
        container.start()
    except Exception as exc:
        pytest.skip(f"docker unreachable for Testcontainers: {exc}")

    try:
        yield container
    finally:
        container.stop()


# ---------------------------------------------------------------------------
# Core per-test engine fixture
# ---------------------------------------------------------------------------


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

    _migrate_fresh(async_url)

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def pg_url(pg_container: PostgresContainer) -> str:
    """Raw asyncpg URL with unmasked password for direct asyncpg use."""
    return _asyncpg_url(pg_container.get_connection_url())


@pytest.fixture
async def pg_engine_noop(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Bare engine pointing at pg_container -- no migration, no seeding.

    Use when another fixture (e.g. ``seeded_app``) already provisioned
    the database and you just need an engine for verification queries.
    """
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_engine_fast_noop(
    pg_container_fast: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Bare engine pointing at pg_container_fast -- no migration, no seeding."""
    sync_url = pg_container_fast.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Slow / fast engine fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_engine_slow(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Engine with migrated schema + slow seed (no indexes)."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def pg_engine_fast(
    pg_container_fast: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    """Engine with migrated schema + fast seed (with indexes)."""
    sync_url = pg_container_fast.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)

    _migrate_fresh(async_url)
    _seed_fast(async_url)

    engine = create_async_engine(async_url, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# App / TestClient fixtures
# ---------------------------------------------------------------------------


def _build_app(
    *,
    demo_mode: bool = True,
) -> Any:
    """Build a FastAPI app using current os.environ settings.

    Callers must ensure DATABASE_URL (and DATABASE_URL_FAST if needed)
    are set in os.environ before calling this.
    """
    from slowquery_demo.main import create_app

    app = create_app()

    # Disable the real engine-builder so branch-switch tests don't
    # try to connect to Neon.
    switcher = getattr(app.state, "branch_switcher", None)
    if switcher is not None:
        switcher._engine_builder = None  # type: ignore[attr-defined]

    return app


@pytest.fixture
def seeded_test_client(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient against an app with migrated + slow-seeded data."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    app = _build_app()

    with TestClient(app) as client:
        yield client


@pytest.fixture
async def sample_user_id(pg_container: PostgresContainer) -> str:
    """Return the ID of an arbitrary user from the seeded DB."""
    from sqlalchemy import text

    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    engine = create_async_engine(async_url, future=True)
    try:
        async with engine.connect() as conn:
            uid = await conn.scalar(text("SELECT id FROM users LIMIT 1"))
        return str(uid)
    finally:
        await engine.dispose()


@pytest.fixture
async def sample_order_id(pg_container: PostgresContainer) -> str:
    """Return the ID of an arbitrary order from the seeded DB."""
    from sqlalchemy import text

    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    engine = create_async_engine(async_url, future=True)
    try:
        async with engine.connect() as conn:
            oid = await conn.scalar(text("SELECT id FROM orders LIMIT 1"))
        return str(oid)
    finally:
        await engine.dispose()


@pytest.fixture
async def sample_product_id(pg_container: PostgresContainer) -> str:
    """Return the ID of an arbitrary product from the seeded DB."""
    from sqlalchemy import text

    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    engine = create_async_engine(async_url, future=True)
    try:
        async with engine.connect() as conn:
            pid = await conn.scalar(text("SELECT id FROM products LIMIT 1"))
        return str(pid)
    finally:
        await engine.dispose()


@pytest.fixture
def seeded_app(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient with slowquery middleware + seeded data."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", async_url)
    # Low threshold so the drainer picks up all queries.
    monkeypatch.setenv("SLOWQUERY_THRESHOLD_MS", "1")

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    app = _build_app()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded_app_slow(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient on slow branch (no indexes) with seeded data."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", async_url)
    monkeypatch.setenv("BRANCH_CURRENT", "slow")
    # Very low threshold so even small-dataset queries trigger EXPLAIN + rules.
    monkeypatch.setenv("SLOWQUERY_THRESHOLD_MS", "1")

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    app = _build_app()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded_app_fast(
    pg_container_fast: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient on fast branch (with indexes) with seeded data."""
    sync_url = pg_container_fast.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", async_url)
    monkeypatch.setenv("BRANCH_CURRENT", "fast")

    _migrate_fresh(async_url)
    _seed_fast(async_url)

    app = _build_app()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def seeded_app_llm(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient with LLM fallback enabled + seeded data."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", async_url)
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL_PRIMARY", "test/model")

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    app = _build_app()

    with TestClient(app) as client:
        yield client


@pytest.fixture
def test_client_dead_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient whose DB pool raises on every query (simulates dead pool)."""
    from slowquery_demo.core.database import get_db
    from slowquery_demo.main import create_app

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test_dead")
    monkeypatch.setenv("DATABASE_URL_FAST", "postgresql+asyncpg://test:test@localhost/test_dead")

    app = create_app()

    # Override get_db to raise an OperationalError (simulates dead pool).
    from sqlalchemy.exc import OperationalError

    async def _dead_db() -> Any:
        raise OperationalError("connection refused", {}, Exception("dead pool"))

    app.dependency_overrides[get_db] = _dead_db

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
def test_client_lifespan(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """TestClient that tracks store.close() calls during shutdown."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", async_url)

    _migrate_fresh(async_url)

    from slowquery_demo.main import create_app

    app = create_app()

    # Spy on the store's close method.
    store = getattr(app.state, "slowquery_store", None)
    close_calls = 0
    if store is not None:
        original_close = store.close

        async def _spy_close() -> None:
            nonlocal close_calls
            close_calls += 1
            await original_close()

        store.close = _spy_close

    client = TestClient(app, raise_server_exceptions=False)
    client.store_close_calls = property(lambda self: close_calls)  # type: ignore[attr-defined]

    # Attach close_calls as a plain attribute on the client for the test to read.
    class _WrappedClient(TestClient):
        @property
        def store_close_calls(self) -> int:  # type: ignore[override]
            return close_calls

    wrapped = _WrappedClient(app, raise_server_exceptions=False)
    return wrapped


@pytest.fixture
def dual_pg_app(
    pg_container: PostgresContainer,
    pg_container_fast: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    """TestClient that supports branch switching between slow/fast containers."""
    slow_url = _asyncpg_url(pg_container.get_connection_url())
    fast_url = _asyncpg_url(pg_container_fast.get_connection_url())
    monkeypatch.setenv("DATABASE_URL", slow_url)
    monkeypatch.setenv("DATABASE_URL_FAST", fast_url)
    monkeypatch.setenv("SLOWQUERY_STORE_URL", slow_url)
    monkeypatch.setenv("SLOWQUERY_THRESHOLD_MS", "1")

    _migrate_fresh(slow_url)
    _seed_slow(slow_url)
    _migrate_fresh(fast_url)
    _seed_fast(fast_url)

    from slowquery_demo.main import create_app

    app = create_app()

    # Wire up a real engine builder that can switch between the two
    # containers. Re-attaches slowquery hooks to the new engine.
    from slowquery_demo.core.database import build_engine

    async def _rebuild(url: str) -> tuple[Any, Any]:
        new_engine, new_factory = build_engine(url)
        old_engine = app.state.engine
        app.state.engine = new_engine
        app.state.db_sessionmaker = new_factory

        # Re-attach slowquery hooks to the new engine so the drainer
        # can observe queries on the switched branch.
        buffer = getattr(app.state, "slowquery_buffer", None)
        if buffer is not None:
            from slowquery_demo.core.observability import _patched_attach

            _patched_attach(new_engine, buffer, sample_rate=1.0)

        await old_engine.dispose()
        return new_engine, new_factory

    app.state.branch_switcher._engine_builder = _rebuild
    app.state.branch_switcher._slow_url = slow_url
    app.state.branch_switcher._fast_url = fast_url

    with TestClient(app) as client:
        yield client


class _LiveDemo:
    """Wrapper around a real uvicorn server for traffic generator tests."""

    def __init__(self, base_url: str, process: subprocess.Popen[bytes]) -> None:
        self._base_url = base_url
        self._process = process

    @property
    def base_url(self) -> str:
        return self._base_url

    def stop(self) -> None:
        self._process.terminate()
        self._process.wait(timeout=10)


def _start_uvicorn_server(
    async_url: str,
    *,
    demo_mode: bool = True,
    env_extra: dict[str, str] | None = None,
) -> _LiveDemo:
    """Boot a real uvicorn server on a free port and return a _LiveDemo."""
    import socket
    import time

    # Find a free port.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = {
        **os.environ,
        "DATABASE_URL": async_url,
        "DATABASE_URL_FAST": async_url,
        "DEMO_MODE": str(demo_mode).lower(),
    }
    if env_extra:
        env.update(env_extra)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "slowquery_demo.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        cwd=_PROJECT_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to be ready (up to 15 seconds).
    base_url = f"http://127.0.0.1:{port}"
    import httpx

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=1)
            if r.status_code == 200:
                return _LiveDemo(base_url, proc)
        except Exception:
            pass
        time.sleep(0.3)

    proc.terminate()
    proc.wait(timeout=5)
    stderr = proc.stderr.read().decode() if proc.stderr else ""
    raise RuntimeError(f"uvicorn did not start within 15s. stderr: {stderr}")


@pytest.fixture
def live_demo(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_LiveDemo]:
    """A real uvicorn server for traffic generator tests."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("DEMO_MODE", "true")

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    demo = _start_uvicorn_server(async_url, demo_mode=True)
    try:
        yield demo
    finally:
        demo.stop()


@pytest.fixture
def live_demo_non_demo(
    pg_container: PostgresContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_LiveDemo]:
    """A real uvicorn server with DEMO_MODE=false."""
    sync_url = pg_container.get_connection_url()
    async_url = _asyncpg_url(sync_url)
    monkeypatch.setenv("DATABASE_URL", async_url)
    monkeypatch.setenv("DATABASE_URL_FAST", async_url)
    monkeypatch.setenv("DEMO_MODE", "false")

    _migrate_fresh(async_url)
    _seed_slow(async_url)

    demo = _start_uvicorn_server(async_url, demo_mode=False)
    try:
        yield demo
    finally:
        demo.stop()
