# Spec 01 — Concrete `StoreWriter`

## Goal

`slowquery-detective` ships `StoreWriter` as an abstract base that raises `NotImplementedError` from every method — see [`slowquery-detective/src/slowquery_detective/store.py`](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/src/slowquery_detective/store.py). This spec defines `slowquery_demo.services.store.PostgresStoreWriter`, the asyncpg-backed subclass that persists fingerprints, samples, plans, and suggestions into the four bookkeeping tables from Spec 00. It is the glue that turns the library's observability data into rows the dashboard can query.

## Inputs / Outputs

- **Constructor:** `PostgresStoreWriter(store_url: str, *, pool: asyncpg.Pool | None = None)`. If `pool` is omitted, the writer owns a private pool built lazily on first use from `store_url`.
- **Hooks to implement (all async, all return `None`):**
  - `upsert_fingerprint(fingerprint_id, canonical_sql)` — upsert into `query_fingerprints`, updating `last_seen` and bumping `call_count`, leaving rolling stats alone (those are updated from samples).
  - `record_sample(fingerprint_id, duration_ms, rows)` — insert into `query_samples` and recompute p50/p95/p99/max/total_ms/call_count on the parent fingerprint in the same transaction.
  - `upsert_plan(fingerprint_id, plan_json, plan_text, cost)` — upsert one row per fingerprint in `explain_plans`.
  - `insert_suggestions(fingerprint_id, suggestions)` — batch-insert into `suggestions`, deduping on `(fingerprint_id, kind, sql)` so repeat rule fires don't pile up.
  - `close()` — dispose of the owned pool (no-op if pool was passed in).

## Invariants

1. Every write is inside a short-lived transaction. No multi-hook transactions — the library calls hooks independently and we don't want a slow EXPLAIN worker holding a row lock on `query_fingerprints` for seconds.
2. Rolling percentile math runs against the last `N` samples (`N = 500`, configurable via `SLOWQUERY_SAMPLE_WINDOW`) to keep the stats bounded even under traffic generator load. Window computed in SQL using `ORDER BY sampled_at DESC LIMIT N`, not in Python, so the writer stays pure async DB calls.
3. The writer never issues DDL. Never `CREATE INDEX`, never `DROP`, never `ALTER`. Schema lives in Alembic.
4. If the underlying pool is dead, hooks raise `StoreWriterError` (wraps the asyncpg exception). The library's `ExplainWorker` is responsible for catching and logging; we don't silently swallow.
5. The writer is safe to construct before the event loop starts (`pool` is built lazily inside the first `async` call), so `install(app, engine, store=PostgresStoreWriter(...))` can be wired at module import.

## Test cases

**Success (unit — mocked asyncpg, pure):**
1. `PostgresStoreWriter(store_url="postgresql://...")` with no pool stores the URL and leaves `self._pool is None`.
2. `upsert_fingerprint` issues a single SQL statement with the canonical `INSERT … ON CONFLICT (id) DO UPDATE SET last_seen = now(), call_count = call_count + 1` shape (verified via recorded SQL fixture).
3. `record_sample` fires two statements in one transaction: the sample insert, and the fingerprint stats recompute. Both use the same connection object.
4. `upsert_plan` uses `ON CONFLICT (fingerprint_id) DO UPDATE` so replaying the same plan is a no-op (idempotent).
5. `insert_suggestions` with an empty list is a no-op (no SQL issued).
6. `insert_suggestions([s1, s2])` issues one `INSERT … VALUES (…), (…)` batch, not two separate statements.
7. `close()` on a writer that never used its pool is a no-op.
8. `close()` on a writer that built its pool calls `pool.close()` exactly once; a second `close()` is a no-op.

**Success (integration — Testcontainers Postgres + real schema, `@pytest.mark.integration`):**
9. Run `alembic upgrade head`, then `upsert_fingerprint("abc123", "SELECT * FROM t WHERE id = ?")` → row present with `call_count = 1`.
10. Calling `upsert_fingerprint` twice bumps `call_count` to 2 and advances `last_seen`.
11. `record_sample` 10 times with known durations → the parent fingerprint's p50/p95/p99 match the numpy quantile of those durations within ±0.5ms.
12. `record_sample` 1000 times → only the most recent `SLOWQUERY_SAMPLE_WINDOW` samples influence the rolling stats (verified by injecting an outlier in the oldest position and confirming it doesn't move p99).
13. `upsert_plan` followed by a second `upsert_plan` for the same fingerprint replaces the row, doesn't insert a duplicate (PK enforcement).
14. `insert_suggestions` with a list containing two suggestions that differ only in `rationale` but share `(fingerprint_id, kind, sql)` inserts one row, not two (dedupe ON CONFLICT DO NOTHING).
15. `close()` releases the pool; a subsequent hook call raises `StoreWriterError`.

**Failure / negative:**
16. `upsert_plan` with a plan_json that is not JSON-serializable raises `StoreWriterError("plan_json must be JSON-serializable")` before any SQL runs.
17. `record_sample` with `duration_ms <= 0` raises `ValueError` — negative durations are impossible and we want to fail loudly.
18. A `StoreWriter` built against a URL whose DB doesn't exist surfaces `StoreWriterError` wrapping the asyncpg connection error on first hook, not at construction time.

**Security / destructive-guard:**
19. No hook accepts user-supplied SQL. `canonical_sql` is passed as a parameter, never interpolated. A test injects `"; DROP TABLE users;--"` into `canonical_sql` and confirms the `users` table still exists afterwards.
20. The writer's SQL statements are exhaustively enumerated in a private constant `_STATEMENTS: Final[dict[str, str]]`. A test asserts the constant keys match the set of hook names and that no hook constructs ad-hoc SQL at runtime (grep test).

## Acceptance

- [ ] `src/slowquery_demo/services/store.py` implements `PostgresStoreWriter(StoreWriter)` with the five hooks.
- [ ] `src/slowquery_demo/services/store_errors.py` defines `StoreWriterError`.
- [ ] `_STATEMENTS` holds all SQL used by the writer as named constants.
- [ ] Unit tests use `asyncpg.Pool` replaced with an `AsyncMock`; no real DB contact.
- [ ] Integration tests run against a Testcontainers Postgres that has had `alembic upgrade head` applied.
- [ ] Every test case above has a corresponding test.
- [ ] `mypy --strict` clean; asyncpg types imported via `asyncpg.Connection`, `asyncpg.Pool`.
