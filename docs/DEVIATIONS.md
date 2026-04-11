# Deviations from `docs/projects/50-slowquery-detective.md`

Complete list of gaps between what the authoritative spec describes and what actually ships in this repo's `v0.1.0`. Each entry names the deviation, the rationale, and the concrete follow-up that would close it.

## 1. Seed size: 100k / 500k instead of 1M / 5M

**Spec says:** `users` (10k), `orders` (1M), `order_items` (5M), `products` (2k).
**Repo ships:** `users` (1k), `orders` (100k), `order_items` (500k), `products` (200).

**Why:** `scripts/_seed_common.build_order_item_rows` constructs every `OrderItemRow` in Python memory before the asyncpg `COPY` call. 5M dataclass instances is roughly 1.5 GB of Python overhead on top of the base interpreter footprint, which is risky on a dev laptop and burns Neon Free compute I'd rather save for live load. The 100k/500k size is large enough for the observability pipeline to produce real rolling percentiles and for the `sort_without_index` rule to fire repeatedly; the 1M/5M number was aspirational.

**To close:** rewrite `build_order_item_rows` as a generator that yields batches of N rows, `COPY` each batch, drop the batch, GC between batches. Re-run the seed scripts at full scale.

## 2. Rules engine: 1 of 3 rules firing on live traffic

**Spec says:** "Rules engine fires on the seeded demo's 3 known-bad queries".
**Repo ships:** `sort_without_index` fires reliably on `ORDER BY created_at` queries; `seq_scan_large_table` and `n_plus_one` do not fire in the current configuration.

**Why `seq_scan_large_table` doesn't fire:** Postgres's cost-based planner chooses Bitmap Index Scan over Seq Scan on 100k-row tables even when the ideal index is missing — it uses whatever indexes are around plus a Bitmap AND. At 1M rows the cost calculus tips toward Seq Scan and the rule would fire. This is a data-size issue (see deviation 1), not a rule bug.

**Why `n_plus_one` doesn't fire:** the rule looks for ≥50 calls to the same fingerprint within 1 second. The traffic generator's N+1 burst task serialises 50 calls in a for-loop, and each round-trip from the Render instance to Neon is ~700 ms under the free-tier network layout, so the burst spreads over ~35 seconds not 1. The rule is correct; the test harness is too slow.

**To close:** scale the seed (fixes 1); rewrite the N+1 burst task to use `asyncio.gather` across 50 concurrent requests, or add a `/debug/n-plus-one-burst` endpoint on the service itself that issues all 50 queries in one handler call.

## 3. `POST /branches/switch` does not actually rebuild the engine

**Spec says:** "Branch switch toggles between slow and fast demos … uses Neon API to swap between the two branches".
**Repo ships:** the endpoint exists, validates `target ∈ {slow, fast}` with a `Literal` type, serialises concurrent switches via `asyncio.Lock`, persists `app.state.branch_current` to a `.branch_state` file so restarts respect the choice, and returns a `SwitchBranchResponse` with `active`, `switched_at`, and `latency_ms`. It does **not** rebuild the SQLAlchemy `AsyncEngine` / `async_sessionmaker` against the fast branch's URL — subsequent queries still hit whichever branch the process started with.

**Why:** S4 wrote `BranchSwitcher` against an `engine_builder` callable so the real engine-rebuild path was always going to be an integration concern. In S5b I prioritised the observability pipeline (four library compatibility shims, hook-to-store bridge, direct EXPLAIN) over the engine rebuild because without the pipeline the demo is entirely dead. With the pipeline working, the engine rebuild is a clean and contained follow-up.

**To close:** plumb an engine-rebuilder callable through `create_app()` into `BranchSwitcher.__init__`. On switch: build a new `AsyncEngine` against the fast URL, run a `SELECT 1` health-check with a timeout, dispose of the old engine with a 5-second grace window, atomically swap `app.state.engine` + `app.state.db_sessionmaker`, clear `app.state.slowquery_buffer` so old-branch percentiles don't pollute the new branch's rolling stats. Spec 06 test 6–10 in the integration lane already exercise this contract end-to-end — they just need the conftest to boot two Testcontainers Postgres instances.

## 4. `SLOWQUERY_THRESHOLD_MS` default is too high for the current data size

**Spec says:** threshold of 100 ms is "any query exceeding the p95 threshold".
**Repo ships:** threshold defaults to 100 ms and queries against the seeded 100k-row dataset often complete under that threshold on Neon, so the drainer's EXPLAIN + rules path runs less often than it should.

**Why:** coupled to deviation 1. The 1200 ms → 18 ms story in the spec assumes data large enough to make sequential scans expensive; at 100k rows the Bitmap Heap Scan path is already cheap.

**To close:** either scale the seed or lower the threshold in Render's env to 10 ms. Both are one-line changes. I've chosen to ship at the spec-default threshold for now and document the gap honestly.

## 5. LLM fallback (`LLM_FALLBACK_ENABLED`) is off in production

**Spec says:** "When the rules don't match, an LLM is asked 'explain why this plan is slow'".
**Repo ships:** `LLM_FALLBACK_ENABLED=false` on the live Render service. The code path is wired (`core/observability._build_llm_config`, library passes the `LlmConfig` through), but the flag is off.

**Why:** the OpenRouter free-tier slug in the [memory pick](https://pypi.org/project/slowquery-detective/) is `nvidia/nemotron-nano-9b-v2:free` and I wanted to validate the rules-only path first before adding an LLM round-trip to the drainer's critical section. The LLM cascade (PRIMARY → FAST → FALLBACK) is well-tested in the library's own unit suite, but I haven't yet observed it live against real Neon data.

**To close:** flip `LLM_FALLBACK_ENABLED=true` in Render env. If rules fire on a plan the drainer will skip the LLM entirely; if they don't, the drainer will call the library's `explain()` function and persist any resulting suggestion alongside rule-produced ones.

## 6. `render.yaml` `preDeployCommand` is silently ignored on Free tier

**Spec says (implicitly):** Render runs `alembic upgrade head` as a pre-deploy step.
**Reality:** `preDeployCommand` is gated behind Render paid tiers. On Free the field is accepted by the YAML parser but never executed. First migration ran manually from a dev machine.

**Full write-up with Option A/B/C trade-off table:** [`docs/RENDER_FREE_TIER_MIGRATIONS.md`](RENDER_FREE_TIER_MIGRATIONS.md).

**To close:** move alembic into the Dockerfile `CMD` when schema churn picks up (see the doc for the exact shape).

## 7. Integration lane is committed but Docker-gated

**Spec implies:** full test matrix runs in CI.
**Repo ships:** 73 unit tests run in CI on every push; 41 integration tests live under `tests/integration/` and are filtered out of the default run. They're committed and would work locally given a running Docker Desktop — the session-scoped `pg_container` fixture boots a Testcontainers Postgres, `alembic upgrade head` runs against it via subprocess to avoid event-loop collision with pytest-asyncio.

**Why:** Docker Desktop wasn't running on my dev laptop during S5a when the conftest landed. Adding a CI job that boots Docker-in-Docker is a separate line item.

**To close:** add a second CI job that runs `uv run pytest -m integration` with a `services.postgres` entry or a Docker-in-Docker setup. The fixture is ready for it.

## 8. Four live production bugs fixed by in-repo shims rather than upstream PRs

**Reality:** [`core/observability.py`](../src/slowquery_demo/core/observability.py) contains four module-level monkey-patches that rewrite slowquery-detective v0.1.0's behaviour. Each is documented inline with a rationale. The right long-term fix is to upstream these to the library itself:

1. Replace `app.add_event_handler` with FastAPI lifespan.
2. Accept an optional `store: StoreWriter | None` parameter on `install()`.
3. Rewrite `hooks.attach` to stash state via `setattr(context)` rather than `cursor.info`.
4. Rewrite `ExplainWorker._run_explain` to accept real captured parameters or integrate the bridge queue pattern from shim 4.

**To close:** open PRs against the `slowquery-detective` repo. Until then, the shims stay in this repo's `core/observability.py` and are versioned alongside the library they patch.

---

## Deviations explicitly accepted for v0.1.0

These are intentional scope cuts that won't be closed in this repo:

- **No live SSE endpoint for the dashboard.** `/_slowquery/queries` returns an empty list right now (my local stub) because the library's `dashboard_router` is a lazy stub. Phase 4c (dashboard frontend) will either read the bookkeeping tables directly or I'll upstream a real router to the library.
- **No separate `slowquery-store` Neon branch.** Bookkeeping tables live on the same branch as the commerce data. The spec hinted at an "admin" branch but the complexity wasn't worth it for a portfolio demo.
- **No EXPLAIN ANALYZE — just EXPLAIN.** The drainer runs `EXPLAIN (FORMAT JSON)` without `ANALYZE` to avoid double-executing the query. `ANALYZE` would add real timings to the plan; without it we get estimated rows and costs which is enough for the rules engine.

See [`docs/projects/50-slowquery-detective.md`](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/docs/projects/50-slowquery-detective.md) for the authoritative spec.
