# Architecture

## Strict MVC layering

```
api/routers     →   HTTP surface (FastAPI; thin; no SQLAlchemy imports)
services/       →   business logic (pure; raises typed DomainError subclasses)
repositories/   →   async SQLAlchemy select()/text() — the ONLY layer that imports SQLAlchemy
models/         →   SQLAlchemy 2.0 DeclarativeBase ORM classes
schemas/        →   Pydantic v2 DTOs + PaginatedResponse[T]
core/           →   config (Settings), database (engine + session factory),
                     errors (DomainError + exception handlers), observability
                     (the four library shims + drainer), platform (health, CORS)
```

Controllers never touch the DB. Models never know about HTTP. Pure core, imperative shell.

## Data plane (the route `/users/{id}/orders` takes)

```mermaid
flowchart TD
    Client[curl / dashboard / traffic_generator]
    Router[api/routers/users.py<br/>list_user_orders]
    Service[services/order_service.py<br/>list_user_orders]
    Repo[repositories/order_repository.py<br/>list_for_user]
    Session[AsyncSession via get_db dependency]
    Engine[AsyncEngine<br/>build_engine with normalise_asyncpg_url]
    Hook[SQLAlchemy event<br/>before/after_cursor_execute]
    Neon1[(Neon slowquery branch<br/>seeded commerce schema)]

    Bridge[Shim 4 bridge queue<br/>asyncio.Queue 10k]
    Drainer[core/observability.py<br/>_drainer task]
    StoreWriter[services/store.py<br/>PostgresStoreWriter]
    DirectExplain[_run_direct_explain<br/>real statement + params]
    Rules[slowquery_detective.rules.run_rules]
    Neon2[(Neon slowquery branch<br/>query_fingerprints / samples /<br/>explain_plans / suggestions)]

    Client --> Router
    Router --> Service
    Service --> Repo
    Repo --> Session
    Session --> Engine
    Engine --> Hook
    Engine --> Neon1
    Hook -. loop.call_soon_threadsafe .-> Bridge
    Bridge --> Drainer
    Drainer --> StoreWriter
    Drainer --> DirectExplain
    DirectExplain --> Rules
    Rules --> StoreWriter
    StoreWriter --> Neon2
```

The dashed arrow from `Hook` to `Bridge` is the only cross-loop boundary: SQLAlchemy events fire in sync context (even for async engines), and we dispatch to the FastAPI event loop via `loop.call_soon_threadsafe`. Everything downstream of the bridge is pure async on the main loop.

## Two Neon branches

```mermaid
flowchart LR
    App[slowquery_demo FastAPI<br/>on Render]
    Switch[branches/switch endpoint<br/>BranchSwitcher service]
    BranchState[(.branch_state file)]
    Slow[(Neon slowquery branch<br/>8 tables, NO indexes on<br/>orders.user_id<br/>order_items.order_id<br/>order_items.product_id)]
    Fast[(Neon slowquery-fast branch<br/>same 8 tables + 3 indexes<br/>via seed_fast.py)]

    App --> Slow
    Switch --> BranchState
    Switch -. planned: rebuild engine .-> Fast
```

The branch-switch code path exists (request body validation, asyncio.Lock, state persistence) but does not yet rebuild the SQLAlchemy engine at runtime against the fast URL — see [DEVIATIONS.md](DEVIATIONS.md).

## Key endpoints

| Surface | Purpose | Slow-path trigger |
|---|---|---|
| `/health` | Liveness probe (platform middleware) | — |
| `/version` | Build identity | — |
| `/_slowquery/queries` | Dashboard API — returns the fingerprint list | — |
| `/users`, `/products` | Fast reads (unique indexes on email / sku) | — |
| `/orders?limit=N` | Recent orders, `ORDER BY created_at DESC` | **sort_without_index** rule |
| `/users/{id}/orders` | Orders for one user | Seq Scan on `orders.user_id` |
| `/orders/{id}` | Order + its items (join to `order_items`) | Seq Scan on `order_items.order_id` |
| `/order_items?product_id=...` | Items for one product | Seq Scan on `order_items.product_id` |
| `/branches/switch` | Swap active branch state (full engine rebuild deferred) | — |

## The four library compatibility shims

See [`core/observability.py`](../src/slowquery_demo/core/observability.py) for the implementation; each shim is documented inline. Summary:

1. `add_event_handler` → no-op (Starlette 1.0 removed it).
2. `StoreWriter` swapped at import time via `setattr(_sqd_middleware, "StoreWriter", PostgresStoreWriter)`.
3. `cursor.info[_KEY]` → `setattr(context, _KEY, ...)` in the hook (async cursors and asyncpg contexts both lack `.info`).
4. Sync-hook to async-store bridge + direct EXPLAIN using real captured statement + parameters, skipping the library's broken `synthesize_params`.

## Migration path

Alembic async env reads `DATABASE_URL` via `slowquery_demo.core.db_config.get_database_url()` which runs the URL through `normalise_asyncpg_url()` so libpq-style `sslmode` / `channel_binding` params don't break asyncpg.

One migration: [`alembic/versions/0001_initial.py`](../alembic/versions/0001_initial.py) — hand-written DDL for all 8 tables + the `order_status` enum. The no-index guard test ([`tests/unit/test_00_schema.py::test_migration_does_not_create_forbidden_indexes`](../tests/unit/test_00_schema.py)) greps this file and fails the build if any future change adds an index on the three demo-critical columns.

On Render Free tier, `render.yaml`'s `preDeployCommand: alembic upgrade head` is [silently ignored](RENDER_FREE_TIER_MIGRATIONS.md), so the first migration ran manually from a dev machine. Subsequent migrations will move into the Dockerfile `CMD` when schema churn picks up.
