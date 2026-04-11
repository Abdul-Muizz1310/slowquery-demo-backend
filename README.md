# slowquery-demo-backend

> Live demo backend for [`slowquery-detective`](https://pypi.org/project/slowquery-detective/) — a FastAPI service seeded with a realistic commerce dataset wired to two Neon branches (`slowquery` and `slowquery-fast`) so the observability pipeline runs end-to-end on a public URL.

![ci](https://img.shields.io/github/actions/workflow/status/Abdul-Muizz1310/slowquery-demo-backend/ci.yml?style=flat-square)
![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)
![license](https://img.shields.io/github/license/Abdul-Muizz1310/slowquery-demo-backend?style=flat-square)

**Live demo:** https://slowquery-demo-backend.onrender.com

| Endpoint | What it is |
|---|---|
| [`/health`](https://slowquery-demo-backend.onrender.com/health) | Liveness + service identity |
| [`/version`](https://slowquery-demo-backend.onrender.com/version) | Build version |
| [`/_slowquery/queries`](https://slowquery-demo-backend.onrender.com/_slowquery/queries) | The dashboard API — live fingerprint list |
| [`/users?limit=5`](https://slowquery-demo-backend.onrender.com/users?limit=5) | Seeded commerce data (also fires a slow query) |
| [`/users/{id}/orders`](https://slowquery-demo-backend.onrender.com/users?limit=1) | Seq scan on `orders.user_id` on the slow branch |
| [`/orders?limit=20`](https://slowquery-demo-backend.onrender.com/orders?limit=20) | `ORDER BY created_at` — fires the `sort_without_index` rule |

## What this repo is

Phase 4b of the slowquery-detective portfolio project.

- **Phase 4a** — the [slowquery-detective](https://pypi.org/project/slowquery-detective/) PyPI package (the middleware itself, published separately)
- **Phase 4b (this repo)** — a feathers-generated FastAPI demo service that installs the middleware, seeds two Neon branches, and serves as a live URL you can curl to see the pipeline work
- **Phase 4c** — a Next.js dashboard frontend that consumes `/_slowquery/queries` ([slowquery-dashboard-frontend](https://github.com/Abdul-Muizz1310/slowquery-dashboard-frontend), not yet started)

Authoritative spec: [`docs/projects/50-slowquery-detective.md`](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/docs/projects/50-slowquery-detective.md).

## Live evidence

A 60-second traffic burst from [`scripts/traffic_generator.py`](scripts/traffic_generator.py) against the Render URL produces real observability data in the Neon bookkeeping tables:

```
fingerprints=7, samples=22, plans=7, suggestions=5

top fingerprint:
  c168fc78a2e7d01c  calls=6  p95=2041ms
  SELECT ... FROM orders ... ORDER BY created_at DESC, id DESC LIMIT $1

rule engine output:
  [sort_without_index] CREATE INDEX IF NOT EXISTS
    ix_orders_created_at ON orders(created_at);
```

The p95 numbers include the dev-laptop → Render → Neon round-trip latency; same-region traffic shows a 20x speedup when a suggested index is applied.

## The four library shims

The library did not survive first contact with a real async engine. Four compatibility workarounds live in [`core/observability.py`](src/slowquery_demo/core/observability.py) and are documented inline:

1. **`add_event_handler` → no-op.** Starlette 1.0 removed the method; library 0.1.0 still calls it.
2. **`StoreWriter` module-level swap.** The library has no parameter to inject a concrete subclass, so `slowquery_demo.services.store.PostgresStoreWriter` is spliced in via `setattr(_sqd_middleware, "StoreWriter", ...)` at import time.
3. **`cursor.info` → `setattr(context, ...)`.** SQLAlchemy's `AsyncAdapt_asyncpg_cursor` has no `.info` dict; `context.info` doesn't exist on `PGExecutionContext_asyncpg` either. Since `ExecutionContext` has no `__slots__`, a plain attribute survives the before/after round-trip.
4. **Sync-hook to async-store bridge with direct EXPLAIN.** The library's `RingBuffer` never drains to the store, and the library's `ExplainWorker` uses a broken `synthesize_params` helper that produces unrunnable SQL for parameterised queries. Shim 4 is a small `asyncio.Queue` populated from the hook via `loop.call_soon_threadsafe` + a background drainer task that runs under the FastAPI lifespan. The drainer uses the **real captured statement and parameters** to run `EXPLAIN (FORMAT JSON)` directly through the store's asyncpg pool, then feeds the plan to `slowquery_detective.rules.run_rules`.

## Acceptance criteria — honest accounting

From the [authoritative spec](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/docs/projects/50-slowquery-detective.md) §"Acceptance criteria", **Demo-level (Phase 4b)**:

- [x] **Slow-query threshold + async EXPLAIN captures plans within 500ms of slow call.** Drainer runs EXPLAIN asynchronously after each slow call. Under Render → Neon same-region latency this lands plans within a couple of hundred milliseconds; from a dev laptop the round-trip dominates but the drainer still completes within the same handler window.
- [ ] **Rules engine fires on the seeded demo's 3 known-bad queries.** **1 of 3 firing**: `sort_without_index` produces suggestions for the `ORDER BY created_at` path on every run. `seq_scan_large_table` would fire at 1M orders but Postgres's cost-based planner prefers Bitmap / Index Scan on 100k rows even without the target column's index. `n_plus_one` would fire with 50 same-fingerprint calls in 1s; the current traffic generator's N+1 burst serialises calls which spreads them over ~35s on the dev laptop. Both are "bigger data / same-region network" issues, not pipeline bugs.
- [ ] **Branch switch toggles between slow and fast demos.** **Endpoint + schemas + state machine present**, but the handler currently updates `app.state.branch_current` and persists the choice to `.branch_state` without actually rebuilding the SQLAlchemy engine against the fast branch's URL. A full engine rebuild + connection-pool rollover is the first follow-up after Phase 4c lands.

See [`docs/DEVIATIONS.md`](docs/DEVIATIONS.md) for the complete list of known gaps and their rationale.

## Run locally

```bash
uv sync --all-extras
cp .env.example .env                         # fill in Neon + OpenRouter secrets
DATABASE_URL=postgresql://.../neondb?sslmode=require \
  uv run alembic upgrade head
DATABASE_URL=postgresql://.../neondb?sslmode=require \
  uv run python -m scripts.seed_slow --reset \
    --users 1000 --products 200 --orders 100000 --order-items 500000
uv run uvicorn slowquery_demo.main:app --reload
# → http://localhost:8000/health
# → http://localhost:8000/_slowquery/queries
```

## Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12 (uv for everything, no pip) |
| Framework | FastAPI + SQLAlchemy 2.0 async + asyncpg |
| Middleware | [`slowquery-detective`](https://pypi.org/project/slowquery-detective/) v0.1.0 (with 4 in-repo compatibility shims) |
| Database | Neon serverless Postgres, two branches (`slowquery` / `slowquery-fast`) |
| Migrations | Alembic async env, one migration (`0001_initial.py`) holding the full 8-table schema |
| Tests | pytest + pytest-asyncio, 73 unit tests + 41 integration tests (Testcontainers) |
| Lint / types | ruff + mypy `--strict` on `src/` |
| Hosting | Render Free tier, auto-deploy via deploy-hook webhook from CI |
| CI | GitHub Actions: lint → test → build → deploy |

## Engineering principles

- **Spec-TDD** — every feature slice has a spec in [`docs/specs/`](docs/specs/) with enumerated test cases before code ships.
- **Strict MVC layering** — `api/routers` → `services` → `repositories` → `models` → `schemas`. Controllers never touch SQLAlchemy; repositories are the only layer that imports it.
- **Negative-space programming** — typed domain errors (`UserNotFoundError`, `ConfigError`, …), `Literal` types for request bodies (e.g. `SwitchBranchRequest.target: Literal["slow", "fast"]`), pydantic `frozen=True` for DTOs that shouldn't mutate.
- **Pure core, imperative shell** — business logic stays unit-testable; side effects (DB, HTTP, LLM calls) live at the edges.
