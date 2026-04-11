# Architecture

> Fully drawn in S2 after specs land. This file is a stub so the repo layout stays honest in S1.

## Layering

```
api/routers    → HTTP surface (FastAPI)
services/      → business logic (pure)
repositories/  → async SQLAlchemy data access
models/        → SQLAlchemy ORM classes
schemas/       → Pydantic v2 DTOs
core/          → config, platform middleware, logging
```

Controllers never touch the DB. Models never know about HTTP. Pure core, imperative shell.

## Data plane

```mermaid
flowchart LR
  Dash[Next.js dashboard] -->|SSE + HTTP| App[slowquery_demo FastAPI]
  App --> MW[slowquery-detective<br/>middleware]
  MW --> Buffer[ring buffer]
  MW --> Worker[explain worker]
  Worker --> Store[(Neon branch<br/>query_fingerprints / explain_plans / suggestions)]
  App --> Demo[(Neon branch<br/>slowquery or slowquery-fast)]
  App --> NeonAPI[Neon API]
  Demo -. switched by .- NeonAPI
```

## Key endpoints

| Surface | Purpose |
|---|---|
| `/health`, `/version` | platform middleware liveness |
| `/users`, `/products`, `/orders`, `/order_items` | demo REST endpoints (seed the traffic) |
| `/_slowquery/queries`, `/_slowquery/stream`, ... | slowquery-detective dashboard API (mounted from the library) |
| `/branches/switch` | swap active Neon branch between `slowquery` and `slowquery-fast` |
