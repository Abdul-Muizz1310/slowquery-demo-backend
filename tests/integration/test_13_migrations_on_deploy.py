"""Integration tests for spec 13 (migration 0002 + migrating every branch).

Real Postgres via Testcontainers. Each test starts from an empty ``public``
schema and leaves one behind, so the downgrade/upgrade fixtures other files
use never inherit a half-migrated database from here.

"Live-shaped" means what the two Neon branches look like: ``0001`` recorded
in ``alembic_version`` but no ``uq_suggestions_fp_kind_sql`` (it was added to
``0001`` after ``0001`` had run there), and duplicate suggestions.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from tests.integration.conftest import _asyncpg_url

if TYPE_CHECKING:
    from testcontainers.postgres import PostgresContainer

pytestmark = pytest.mark.integration

_ROOT = str(Path(__file__).resolve().parents[2])
_CONSTRAINT = "uq_suggestions_fp_kind_sql"


def _alembic(url: str, *args: str) -> None:
    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr}"


async def _reset(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))


async def _constraint_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        count = await conn.scalar(
            text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conname = :name AND conrelid = 'suggestions'::regclass"
            ),
            {"name": _CONSTRAINT},
        )
    return int(count or 0)


async def _version(engine: AsyncEngine) -> str | None:
    async with engine.connect() as conn:
        value = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    return None if value is None else str(value)


async def _suggestion_rows(engine: AsyncEngine) -> list[tuple[int, str, str, str | None]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT id, fingerprint_id, kind, sql FROM suggestions ORDER BY id")
        )
        return [(int(r[0]), str(r[1]), str(r[2]), r[3]) for r in rows]


@pytest.fixture
def slow_url(pg_container: PostgresContainer) -> str:
    return _asyncpg_url(pg_container.get_connection_url())


@pytest.fixture
def fast_url(pg_container_fast: PostgresContainer) -> str:
    return _asyncpg_url(pg_container_fast.get_connection_url())


@pytest.fixture
async def empty_db(slow_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(slow_url, future=True)
    await _reset(engine)
    try:
        yield engine
    finally:
        await _reset(engine)
        await engine.dispose()


async def _make_live_shaped(engine: AsyncEngine, url: str) -> dict[str, list[int]]:
    """``0001`` without the constraint, plus duplicates. Returns the ids per group."""
    _alembic(url, "upgrade", "0001")
    async with engine.begin() as conn:
        await conn.execute(text(f"ALTER TABLE suggestions DROP CONSTRAINT {_CONSTRAINT}"))
        await conn.execute(
            text(
                "INSERT INTO query_fingerprints (id, fingerprint) VALUES "
                "('aaaaaaaaaaaaaaaa', 'select a'), ('bbbbbbbbbbbbbbbb', 'select b')"
            )
        )
        rows = [
            # group "dup": three copies of one index suggestion on fingerprint a
            ("aaaaaaaaaaaaaaaa", "index", "CREATE INDEX ix ON orders(created_at);"),
            ("aaaaaaaaaaaaaaaa", "index", "CREATE INDEX ix ON orders(created_at);"),
            ("aaaaaaaaaaaaaaaa", "index", "CREATE INDEX ix ON orders(created_at);"),
            # same sql, different kind: not a duplicate
            ("aaaaaaaaaaaaaaaa", "rewrite", "CREATE INDEX ix ON orders(created_at);"),
            # same sql and kind, different fingerprint: not a duplicate
            ("bbbbbbbbbbbbbbbb", "index", "CREATE INDEX ix ON orders(created_at);"),
            # a NULL-sql pair: duplicates of each other
            ("bbbbbbbbbbbbbbbb", "rewrite", None),
            ("bbbbbbbbbbbbbbbb", "rewrite", None),
            # an empty-sql pair (what the store writer stores for "no DDL")
            ("bbbbbbbbbbbbbbbb", "denormalize", ""),
            ("bbbbbbbbbbbbbbbb", "denormalize", ""),
        ]
        ids: list[int] = []
        for fp, kind, sql in rows:
            new_id = await conn.scalar(
                text(
                    "INSERT INTO suggestions (fingerprint_id, kind, sql, source, rationale) "
                    "VALUES (:fp, :kind, :sql, 'rules', 'r') RETURNING id"
                ),
                {"fp": fp, "kind": kind, "sql": sql},
            )
            ids.append(int(new_id))
    return {
        "dup": ids[0:3],
        "other_kind": ids[3:4],
        "other_fp": ids[4:5],
        "null_pair": ids[5:7],
        "empty_pair": ids[7:9],
    }


async def test_fresh_database_ends_at_0002_with_one_constraint(
    empty_db: AsyncEngine, slow_url: str
) -> None:
    """Spec 13 test 14."""
    _alembic(slow_url, "upgrade", "head")
    assert await _version(empty_db) == "0002"
    assert await _constraint_count(empty_db) == 1


async def test_live_shaped_database_is_deduplicated_and_constrained(
    empty_db: AsyncEngine, slow_url: str
) -> None:
    """Spec 13 test 15 / invariants 2 and 4."""
    groups = await _make_live_shaped(empty_db, slow_url)
    assert await _constraint_count(empty_db) == 0

    _alembic(slow_url, "upgrade", "head")

    remaining = {row[0] for row in await _suggestion_rows(empty_db)}
    assert remaining == {
        min(groups["dup"]),
        *groups["other_kind"],
        *groups["other_fp"],
        min(groups["null_pair"]),
        min(groups["empty_pair"]),
    }
    assert await _constraint_count(empty_db) == 1
    assert await _version(empty_db) == "0002"


async def test_conflict_insert_works_after_0002(empty_db: AsyncEngine, slow_url: str) -> None:
    """Spec 13 test 16: the 0.1 store writer's ON CONFLICT insert stops failing.

    Since spec 15 the demo writes through slowquery-detective's own schema;
    the statement the 0.1-era writer ran is replayed here as SQL.
    """
    from sqlalchemy.exc import IntegrityError

    await _make_live_shaped(empty_db, slow_url)
    _alembic(slow_url, "upgrade", "head")
    before = len(await _suggestion_rows(empty_db))

    conflict_insert = text(
        "INSERT INTO suggestions (fingerprint_id, kind, sql, source, rationale) "
        "VALUES ('aaaaaaaaaaaaaaaa', 'index', 'CREATE INDEX ix ON orders(created_at);', "
        "'rules', 'r') ON CONFLICT (fingerprint_id, kind, sql) DO NOTHING"
    )
    for _ in range(2):
        async with empty_db.begin() as conn:
            await conn.execute(conflict_insert)
    assert len(await _suggestion_rows(empty_db)) == before

    with pytest.raises(IntegrityError):
        async with empty_db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO suggestions (fingerprint_id, kind, sql, source, rationale) "
                    "VALUES ('aaaaaaaaaaaaaaaa', 'index', "
                    "'CREATE INDEX ix ON orders(created_at);', 'rules', 'r')"
                )
            )


@pytest.mark.parametrize("shape", ["fresh", "live"])
async def test_rerunning_is_a_no_op(empty_db: AsyncEngine, slow_url: str, shape: str) -> None:
    """Spec 13 test 17 / invariant 3: head twice, and 0002 itself twice."""
    if shape == "live":
        await _make_live_shaped(empty_db, slow_url)
    _alembic(slow_url, "upgrade", "head")
    after_first = await _suggestion_rows(empty_db)

    _alembic(slow_url, "upgrade", "head")
    _alembic(slow_url, "stamp", "0001")
    _alembic(slow_url, "upgrade", "head")

    assert await _suggestion_rows(empty_db) == after_first
    assert await _constraint_count(empty_db) == 1
    assert await _version(empty_db) == "0002"


@pytest.mark.parametrize("shape", ["fresh", "live"])
async def test_downgrade_base_after_head(empty_db: AsyncEngine, slow_url: str, shape: str) -> None:
    """Spec 13 test 18."""
    if shape == "live":
        await _make_live_shaped(empty_db, slow_url)
    _alembic(slow_url, "upgrade", "head")
    _alembic(slow_url, "downgrade", "base")
    async with empty_db.connect() as conn:
        exists = await conn.scalar(text("SELECT to_regclass('public.suggestions')"))
    assert exists is None


@pytest.fixture
def two_empty_databases(slow_url: str, fast_url: str) -> Iterator[tuple[str, str]]:
    import asyncio

    async def _reset_both() -> None:
        for url in (slow_url, fast_url):
            engine = create_async_engine(url, future=True)
            try:
                await _reset(engine)
            finally:
                await engine.dispose()

    asyncio.run(_reset_both())
    try:
        yield slow_url, fast_url
    finally:
        asyncio.run(_reset_both())


def test_migrate_all_brings_slow_and_fast_to_head(two_empty_databases: tuple[str, str]) -> None:
    """Spec 13 test 19: both branches, one run."""
    import asyncio

    slow, fast = two_empty_databases
    env = {**os.environ, "DATABASE_URL": slow, "DATABASE_URL_FAST": fast}
    env.pop("SLOWQUERY_STORE_URL", None)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.migrate_all"],
        env=env,
        capture_output=True,
        text=True,
        cwd=_ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DATABASE_URL_FAST" in result.stdout
    for url in (slow, fast):
        assert url not in result.stdout + result.stderr
    assert "@" not in result.stdout

    async def _versions() -> list[str | None]:
        found: list[str | None] = []
        for url in (slow, fast):
            engine = create_async_engine(url, future=True)
            try:
                found.append(await _version(engine))
                assert await _constraint_count(engine) == 1
            finally:
                await engine.dispose()
        return found

    assert asyncio.run(_versions()) == ["0002", "0002"]
