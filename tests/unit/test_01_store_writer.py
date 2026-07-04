"""S3 red: unit tests for spec 01 (PostgresStoreWriter).

Asyncpg is fully mocked; no real DB contact. See
``docs/specs/01-store-writer.md`` for the enumerated cases.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pool_mock() -> tuple[MagicMock, AsyncMock]:
    """Return a (pool, acquired_conn) pair where ``pool.acquire`` yields the conn."""
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.close = AsyncMock()
    return pool, conn


def test_constructor_defers_pool_creation() -> None:
    """Spec 01 test 1."""
    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url="postgresql://localhost/x")
    assert writer._store_url == "postgresql://localhost/x"  # type: ignore[attr-defined]
    assert writer._pool is None  # type: ignore[attr-defined]


async def test_upsert_fingerprint_issues_expected_sql_shape() -> None:
    """Spec 01 test 2."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    await writer.upsert_fingerprint("abc123", "SELECT * FROM t WHERE id = ?")

    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO query_fingerprints" in sql
    assert "ON CONFLICT (id)" in sql
    assert "call_count = query_fingerprints.call_count + 1" in sql
    assert "last_seen = now()" in sql


async def test_record_sample_uses_single_transaction() -> None:
    """Spec 01 test 3."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    transaction_ctx = AsyncMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)

    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.record_sample("abc123", duration_ms=42.0, rows=10)

    conn.transaction.assert_called_once()
    assert conn.execute.await_count >= 2, "sample insert + stats recompute must fire"


async def test_upsert_plan_is_idempotent_via_on_conflict() -> None:
    """Spec 01 test 4."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    plan: dict[str, Any] = {"Plan": {"Node Type": "Seq Scan"}}

    await writer.upsert_plan("abc123", plan_json=plan, plan_text="text", cost=1.0)

    sql = conn.execute.await_args.args[0]
    assert "INSERT INTO explain_plans" in sql
    assert "ON CONFLICT (fingerprint_id) DO UPDATE" in sql


async def test_insert_suggestions_empty_list_is_noop() -> None:
    """Spec 01 test 5."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    await writer.insert_suggestions("abc123", [])
    conn.execute.assert_not_awaited()


async def test_insert_suggestions_batch_insert_one_statement() -> None:
    """Spec 01 test 6."""
    from slowquery_detective.rules.base import Suggestion

    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    suggestions = [
        Suggestion(
            kind="index", sql="CREATE INDEX ...", rationale="r1", confidence=0.9, source="rules"
        ),
        Suggestion(
            kind="index", sql="CREATE INDEX ...", rationale="r2", confidence=0.8, source="rules"
        ),
    ]

    await writer.insert_suggestions("abc123", suggestions)
    assert conn.execute.await_count == 1, "batch insert must be one round-trip"


async def test_close_without_pool_is_noop() -> None:
    """Spec 01 test 7."""
    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url="postgresql://x")
    await writer.close()  # no exception


async def test_close_disposes_owned_pool_once() -> None:
    """Spec 01 test 8."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, _ = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.close()
    await writer.close()
    pool.close.assert_awaited_once()


async def test_upsert_plan_rejects_non_json_serializable() -> None:
    """Spec 01 test 16."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, _ = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    class _NotJson:
        pass

    with pytest.raises(StoreWriterError, match="JSON"):
        await writer.upsert_plan("abc", plan_json={"x": _NotJson()}, plan_text="", cost=1.0)


async def test_record_sample_rejects_nonpositive_duration() -> None:
    """Spec 01 test 17."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, _ = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(ValueError):
        await writer.record_sample("abc", duration_ms=0.0, rows=None)
    with pytest.raises(ValueError):
        await writer.record_sample("abc", duration_ms=-5.0, rows=None)


async def test_lazy_pool_connection_failure_surfaces_as_store_error() -> None:
    """Spec 01 test 18."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    writer = PostgresStoreWriter(store_url="postgresql://bad-host:1/none")
    with pytest.raises(StoreWriterError):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_canonical_sql_is_bound_parameter_not_string_interp() -> None:
    """Spec 01 test 19: injection guard."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    payload = "'; DROP TABLE users;--"
    await writer.upsert_fingerprint("abc", payload)

    # conn.execute is called with (sql, *params) — payload must appear in params,
    # not interpolated into the sql string.
    sql = conn.execute.await_args.args[0]
    params = conn.execute.await_args.args[1:]
    assert payload not in sql, "canonical_sql leaked into the SQL text"
    assert payload in params, "canonical_sql must be passed as a bind parameter"


async def test_record_sample_skips_percentile_recompute_when_throttled() -> None:
    """OPT-1: recompute_stats=False bumps totals via the cheap statement, no percentile_cont."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    transaction_ctx = AsyncMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)

    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.record_sample("abc123", duration_ms=42.0, recompute_stats=False)

    executed = [call.args[0] for call in conn.execute.await_args_list]
    assert any("INSERT INTO query_samples" in s for s in executed)
    # The bump path must not run the expensive percentile recompute.
    assert not any("percentile_cont" in s for s in executed)
    assert any(s.startswith("UPDATE query_fingerprints SET total_ms") for s in executed)


async def test_record_sample_recompute_runs_percentiles() -> None:
    """Default path still recomputes percentiles (dashboard freshness)."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    transaction_ctx = AsyncMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)

    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.record_sample("abc123", duration_ms=42.0, recompute_stats=True)

    executed = [call.args[0] for call in conn.execute.await_args_list]
    assert any("percentile_cont" in s for s in executed)


async def test_prune_samples_noop_for_nonpositive_retention() -> None:
    """COST-1: retention <= 0 disables pruning without touching the pool."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    await writer.prune_samples(0.0)
    conn.execute.assert_not_awaited()


async def test_prune_samples_deletes_old_rows() -> None:
    """COST-1: a positive retention issues the DELETE with the retention arg."""
    from slowquery_demo.services.store import PostgresStoreWriter

    pool, conn = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    await writer.prune_samples(3600.0)
    conn.execute.assert_awaited_once()
    sql = conn.execute.await_args.args[0]
    assert "DELETE FROM query_samples" in sql
    assert conn.execute.await_args.args[1] == 3600.0


async def test_ensure_pool_builds_pool_only_once_under_concurrency() -> None:
    """MEDIUM: concurrent first-callers must not each build (and leak) a pool."""
    import asyncio

    from slowquery_demo.services import store as store_mod
    from slowquery_demo.services.store import PostgresStoreWriter

    calls = {"n": 0}

    async def _fake_create_pool(**_kwargs: object) -> object:
        calls["n"] += 1
        await asyncio.sleep(0)  # yield so both callers race inside the lock
        return MagicMock(name="pool")

    writer = PostgresStoreWriter(store_url="postgresql://localhost/x")
    import unittest.mock

    with unittest.mock.patch.object(
        store_mod.asyncpg, "create_pool", new=AsyncMock(side_effect=_fake_create_pool)
    ):
        pools = await asyncio.gather(writer._ensure_pool(), writer._ensure_pool())

    assert calls["n"] == 1, "pool built exactly once under concurrent first-callers"
    assert pools[0] is pools[1]


async def test_operations_after_close_raise_store_error() -> None:
    """Spec 01 test 15: a hook call after ``close()`` raises ``StoreWriterError``."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, _ = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.close()

    with pytest.raises(StoreWriterError, match="closed"):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_upsert_fingerprint_wraps_db_errors() -> None:
    """Spec 01 invariant 4: a dead pool surfaces as a typed ``StoreWriterError``."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(StoreWriterError, match="upsert_fingerprint failed"):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_record_sample_wraps_db_errors() -> None:
    """Spec 01 invariant 4: ``record_sample`` wraps the underlying asyncpg error."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    transaction_ctx = AsyncMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(StoreWriterError, match="record_sample failed"):
        await writer.record_sample("abc", duration_ms=10.0)


async def test_upsert_plan_wraps_db_errors() -> None:
    """Spec 01 invariant 4: ``upsert_plan`` wraps the underlying asyncpg error."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(StoreWriterError, match="upsert_plan failed"):
        await writer.upsert_plan("abc", plan_json={"x": 1}, plan_text="text", cost=1.0)


async def test_insert_suggestions_wraps_db_errors() -> None:
    """Spec 01 invariant 4: ``insert_suggestions`` wraps the underlying asyncpg error."""
    from slowquery_detective.rules.base import Suggestion

    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    suggestions = [
        Suggestion(
            kind="index", sql="CREATE INDEX ...", rationale="r", confidence=0.9, source="rules"
        ),
    ]
    with pytest.raises(StoreWriterError, match="insert_suggestions failed"):
        await writer.insert_suggestions("abc", suggestions)


def test_statements_constant_enumerates_all_sql() -> None:
    """Spec 01 test 20: grep guard — no ad-hoc SQL construction."""
    import inspect

    from slowquery_demo.services import store as store_mod

    src = inspect.getsource(store_mod)
    # Any SQL keyword appearing in a source line that isn't the _STATEMENTS
    # constant is a red flag. Cheap heuristic: every "INSERT INTO"/"UPDATE "/"DELETE "
    # string must be inside the constant.
    assert hasattr(store_mod, "_STATEMENTS"), "_STATEMENTS constant must exist"
    statements: dict[str, str] = store_mod._STATEMENTS  # type: ignore[attr-defined]
    expected_keys = {
        "upsert_fingerprint",
        "record_sample",
        "upsert_plan",
        "insert_suggestions",
    }
    assert expected_keys.issubset(set(statements.keys()))
    # Outside the constant, no INSERT/UPDATE/DELETE string literals allowed.
    outside_constant = "\n".join(line for line in src.splitlines() if "_STATEMENTS" not in line)
    for kw in ("INSERT INTO", "UPDATE query_", "DELETE FROM"):
        assert kw not in outside_constant, f"{kw} must live in _STATEMENTS only"
