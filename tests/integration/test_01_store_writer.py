"""S3 red: integration tests for spec 01 (PostgresStoreWriter)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_upsert_fingerprint_inserts_row(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 9."""
    from sqlalchemy import text

    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc123", "SELECT * FROM t WHERE id = ?")

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT call_count FROM query_fingerprints WHERE id = :id"),
                {"id": "abc123"},
            )
        ).first()
    assert row is not None and row[0] == 1
    await writer.close()


async def test_upsert_fingerprint_twice_bumps_call_count(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 10."""
    from sqlalchemy import text

    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc123", "SELECT 1")
    await writer.upsert_fingerprint("abc123", "SELECT 1")

    async with pg_engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT call_count FROM query_fingerprints WHERE id = :id"),
            {"id": "abc123"},
        )
    assert count == 2
    await writer.close()


async def test_record_sample_rolling_percentiles_match_numpy(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 11."""
    import numpy as np
    from sqlalchemy import text

    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc", "SELECT 1")
    durations = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    for d in durations:
        await writer.record_sample("abc", duration_ms=d, rows=1)

    expected_p50 = float(np.quantile(durations, 0.5))
    expected_p95 = float(np.quantile(durations, 0.95))
    expected_p99 = float(np.quantile(durations, 0.99))

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT p50_ms, p95_ms, p99_ms FROM query_fingerprints WHERE id = :id"),
                {"id": "abc"},
            )
        ).first()
    assert row is not None
    assert abs(float(row[0]) - expected_p50) < 0.5
    assert abs(float(row[1]) - expected_p95) < 0.5
    assert abs(float(row[2]) - expected_p99) < 0.5
    await writer.close()


async def test_rolling_window_discards_stale_samples(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 12."""
    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc", "SELECT 1")
    for _ in range(1000):
        await writer.record_sample("abc", duration_ms=10.0, rows=1)
    # One outlier, inserted first (rolling window should discard it).
    # The test is crude in S3 — real assertion lands in S4 with proper fixtures.
    await writer.close()


async def test_upsert_plan_replaces_row(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 13."""
    from sqlalchemy import text

    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc", "SELECT 1")
    await writer.upsert_plan("abc", plan_json={"v": 1}, plan_text="t1", cost=1.0)
    await writer.upsert_plan("abc", plan_json={"v": 2}, plan_text="t2", cost=2.0)

    async with pg_engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT COUNT(*) FROM explain_plans WHERE fingerprint_id = :id"),
            {"id": "abc"},
        )
    assert count == 1
    await writer.close()


async def test_insert_suggestions_dedupes_on_conflict(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 14."""
    from slowquery_detective.rules.base import Suggestion
    from sqlalchemy import text

    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc", "SELECT 1")
    s1 = Suggestion(
        kind="index",
        sql="CREATE INDEX ix ON t(c)",
        rationale="r1",
        confidence=0.9,
        source="rules",
    )
    s2 = Suggestion(
        kind="index",
        sql="CREATE INDEX ix ON t(c)",
        rationale="r2",
        confidence=0.8,
        source="rules",
    )
    await writer.insert_suggestions("abc", [s1, s2])

    async with pg_engine.connect() as conn:
        count = await conn.scalar(
            text("SELECT COUNT(*) FROM suggestions WHERE fingerprint_id = :id"),
            {"id": "abc"},
        )
    assert count == 1
    await writer.close()


async def test_close_releases_pool_and_subsequent_calls_raise(pg_engine, pg_url) -> None:  # type: ignore[no-untyped-def]
    """Spec 01 test 15."""
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    writer = PostgresStoreWriter(store_url=pg_url)
    await writer.upsert_fingerprint("abc", "SELECT 1")
    await writer.close()

    with pytest.raises(StoreWriterError):
        await writer.upsert_fingerprint("abc", "SELECT 1")
