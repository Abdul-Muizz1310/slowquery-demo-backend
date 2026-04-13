"""Concrete asyncpg-backed :class:`StoreWriter` for slowquery-detective.

The library ships ``StoreWriter`` as an abstract base whose methods raise
``NotImplementedError``. This module provides the one concrete
implementation: a pool-backed writer that persists fingerprints,
samples, plans, and suggestions into the four bookkeeping tables from
:mod:`slowquery_demo.models.slowquery_store`.

Design notes
------------
* Every SQL statement used by this module lives in a ``_STATEMENTS_*``
  module-level constant so test 20 (grep guard) can prove nothing
  constructs SQL ad-hoc inside a method body.
* Pool creation is deferred: constructing the writer does not open a
  connection. The first hook call builds the pool. This lets
  ``install(app, engine, store=PostgresStoreWriter(...))`` run at
  module import, before an event loop exists.
* Every public method is short-lived and non-transactional except
  :meth:`record_sample`, which runs two statements (insert + stats
  recompute) inside a single ``conn.transaction()``.
"""

from __future__ import annotations

import json
from typing import Any, Final

import asyncpg
from slowquery_detective.rules.base import Suggestion
from slowquery_detective.store import StoreWriter

from slowquery_demo.services.store_errors import StoreWriterError

# --- SQL statements (every literal lives on a _STATEMENTS line) ---------
# ``# fmt: skip`` pins each line so ``ruff format`` can't wrap a long string
# into a parenthesized multi-line form — test 20 (spec 01) greps for every
# SQL keyword on lines that do **not** contain ``_STATEMENTS`` and fails
# the build if any appear, so these literals must stay on a single line
# that starts with the ``_STATEMENTS_*`` variable name.

_STATEMENTS_UPSERT_FINGERPRINT = "INSERT INTO query_fingerprints (id, fingerprint) VALUES ($1, $2) ON CONFLICT (id) DO UPDATE SET last_seen = now(), call_count = query_fingerprints.call_count + 1"  # fmt: skip
_STATEMENTS_RECORD_SAMPLE = "INSERT INTO query_samples (fingerprint_id, duration_ms, rows) VALUES ($1, $2, $3)"  # fmt: skip
_STATEMENTS_RECORD_SAMPLE_STATS = "WITH recent AS (SELECT duration_ms FROM query_samples WHERE fingerprint_id = $1 ORDER BY sampled_at DESC LIMIT $2) UPDATE query_fingerprints SET p50_ms = (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY duration_ms) FROM recent), p95_ms = (SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_ms) FROM recent), p99_ms = (SELECT percentile_cont(0.99) WITHIN GROUP (ORDER BY duration_ms) FROM recent), max_ms = (SELECT max(duration_ms) FROM recent), total_ms = total_ms + $3::bigint, last_seen = now() WHERE id = $1"  # fmt: skip
_STATEMENTS_UPSERT_PLAN = "INSERT INTO explain_plans (fingerprint_id, plan_json, plan_text, cost, captured_at) VALUES ($1, $2, $3, $4, now()) ON CONFLICT (fingerprint_id) DO UPDATE SET plan_json = EXCLUDED.plan_json, plan_text = EXCLUDED.plan_text, cost = EXCLUDED.cost, captured_at = now()"  # fmt: skip
_STATEMENTS_INSERT_SUGGESTIONS = "INSERT INTO suggestions (fingerprint_id, kind, sql, source, rationale) SELECT $1, unnest($2::text[]), unnest($3::text[]), unnest($4::text[]), unnest($5::text[]) ON CONFLICT (fingerprint_id, kind, sql) DO NOTHING"  # fmt: skip

_STATEMENTS: Final[dict[str, str]] = {
    "upsert_fingerprint": _STATEMENTS_UPSERT_FINGERPRINT,
    "record_sample": _STATEMENTS_RECORD_SAMPLE,
    "record_sample_stats": _STATEMENTS_RECORD_SAMPLE_STATS,
    "upsert_plan": _STATEMENTS_UPSERT_PLAN,
    "insert_suggestions": _STATEMENTS_INSERT_SUGGESTIONS,
}

# Window of recent samples used for rolling percentiles.
_DEFAULT_SAMPLE_WINDOW: Final[int] = 500


class PostgresStoreWriter(StoreWriter):
    """Asyncpg-backed writer persisting observability data."""

    def __init__(
        self,
        store_url: str,
        *,
        pool: Any | None = None,
        sample_window: int = _DEFAULT_SAMPLE_WINDOW,
    ) -> None:
        super().__init__(store_url)
        self._store_url = store_url
        self._pool: Any | None = pool
        self._owns_pool: bool = pool is None
        self._closed: bool = False
        self._sample_window = sample_window

    # --- lifecycle ------------------------------------------------------

    async def _ensure_pool(self) -> Any:
        if self._closed:
            raise StoreWriterError("store writer is closed")
        if self._pool is not None:
            return self._pool
        # Library passes ``store_url`` through as-is from Settings, which
        # may carry the SQLAlchemy ``+asyncpg`` dialect suffix and libpq
        # query params. asyncpg rejects both, so normalise here.
        from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

        dsn = to_raw_asyncpg_dsn(self._store_url)
        try:
            self._pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=4)
        except Exception as exc:  # pragma: no cover - exercised via integration
            raise StoreWriterError(f"failed to build asyncpg pool: {exc}") from exc
        return self._pool

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._pool is not None:
            await self._pool.close()

    # --- hooks ----------------------------------------------------------

    async def upsert_fingerprint(
        self,
        fingerprint_id: str,
        canonical_sql: str,
    ) -> None:
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(_STATEMENTS["upsert_fingerprint"], fingerprint_id, canonical_sql)
        except StoreWriterError:
            raise
        except Exception as exc:
            raise StoreWriterError(f"upsert_fingerprint failed: {exc}") from exc

    async def record_sample(
        self,
        fingerprint_id: str,
        duration_ms: float,
        rows: int | None = None,
    ) -> None:
        if duration_ms <= 0:
            raise ValueError("duration_ms must be > 0")
        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(_STATEMENTS["record_sample"], fingerprint_id, duration_ms, rows)
                await conn.execute(
                    _STATEMENTS["record_sample_stats"],
                    fingerprint_id,
                    self._sample_window,
                    int(duration_ms),
                )
        except StoreWriterError:
            raise
        except Exception as exc:
            raise StoreWriterError(f"record_sample failed: {exc}") from exc

    async def upsert_plan(
        self,
        fingerprint_id: str,
        plan_json: dict[str, Any],
        plan_text: str,
        cost: float,
    ) -> None:
        try:
            plan_json_text = json.dumps(plan_json)
        except (TypeError, ValueError) as exc:
            raise StoreWriterError(f"plan_json must be JSON-serializable: {exc}") from exc

        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    _STATEMENTS["upsert_plan"],
                    fingerprint_id,
                    plan_json_text,
                    plan_text,
                    cost,
                )
        except StoreWriterError:
            raise
        except Exception as exc:
            raise StoreWriterError(f"upsert_plan failed: {exc}") from exc

    async def insert_suggestions(
        self,
        fingerprint_id: str,
        suggestions: list[Suggestion],
    ) -> None:
        if not suggestions:
            return
        kinds = [s.kind for s in suggestions]
        sqls = [s.sql or "" for s in suggestions]
        sources = [s.source for s in suggestions]
        rationales = [s.rationale for s in suggestions]

        pool = await self._ensure_pool()
        try:
            async with pool.acquire() as conn:
                await conn.execute(
                    _STATEMENTS["insert_suggestions"],
                    fingerprint_id,
                    kinds,
                    sqls,
                    sources,
                    rationales,
                )
        except StoreWriterError:
            raise
        except Exception as exc:
            raise StoreWriterError(f"insert_suggestions failed: {exc}") from exc
