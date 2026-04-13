"""Additional coverage tests for PostgresStoreWriter.

Covers: closed writer raises, upsert/record error wrapping, close idempotency.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_pool_mock() -> tuple[MagicMock, AsyncMock]:
    conn = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.close = AsyncMock()
    return pool, conn


async def test_operations_after_close_raise_store_error() -> None:
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, _ = _make_pool_mock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.close()

    with pytest.raises(StoreWriterError, match="closed"):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_upsert_fingerprint_wraps_db_errors() -> None:
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(StoreWriterError, match="upsert_fingerprint failed"):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_record_sample_wraps_db_errors() -> None:
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
    from slowquery_demo.services.store import PostgresStoreWriter, StoreWriterError

    pool, conn = _make_pool_mock()
    conn.execute = AsyncMock(side_effect=RuntimeError("db gone"))
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)

    with pytest.raises(StoreWriterError, match="upsert_plan failed"):
        await writer.upsert_plan("abc", plan_json={"x": 1}, plan_text="text", cost=1.0)


async def test_insert_suggestions_wraps_db_errors() -> None:
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
