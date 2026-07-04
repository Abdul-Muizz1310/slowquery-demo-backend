# 🔬 `slowquery-demo-backend`

> ⚡ **FastAPI demo service for the slowquery-detective observability pipeline.**
> Seeded commerce dataset, two Neon branches (slow vs fast), real EXPLAIN plans, and a live SSE stream — all on a public URL.

🌐 [Live API](https://slowquery-demo-backend.onrender.com) · 📖 [OpenAPI](https://slowquery-demo-backend.onrender.com/docs) · 📦 [slowquery-detective](https://pypi.org/project/slowquery-detective/) · 🖥️ [Dashboard](https://github.com/Abdul-Muizz1310/slowquery-dashboard-frontend) · 📐 [Specs](docs/specs/)

![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)
![fastapi](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![sqlalchemy](https://img.shields.io/badge/SQLAlchemy-2.0%20async-d71f00?style=flat-square)
![neon](https://img.shields.io/badge/Neon-Postgres-00e599?style=flat-square&logo=postgresql&logoColor=white)
![tests](https://img.shields.io/badge/tests-231%20(182u%20+%2049i)-6e9f18?style=flat-square)
![mypy](https://img.shields.io/badge/mypy-strict-blue?style=flat-square)
[![ci](https://github.com/Abdul-Muizz1310/slowquery-demo-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/Abdul-Muizz1310/slowquery-demo-backend/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square)

---

```console
$ curl https://slowquery-demo-backend.onrender.com/_slowquery/queries | jq '.[:2]'
[
  {"fingerprint":"c168fc78","calls":6,"p95_ms":2041,
   "sql":"SELECT … FROM orders ORDER BY created_at DESC LIMIT $1",
   "suggestions":["CREATE INDEX ix_orders_created_at ON orders(created_at)"]},
  {"fingerprint":"a3e9b11d","calls":4,"p95_ms":1847,
   "sql":"SELECT … FROM orders WHERE user_id = $1",
   "suggestions":["CREATE INDEX ix_orders_user_id ON orders(user_id)"]}
]

$ python -m scripts.traffic_generator --burst 60
fingerprints=7  samples=22  plans=7  suggestions=5
top: c168fc78  calls=6  p95=2041ms  rule=sort_without_index
```

---

## 🎯 Why this exists

Phase 4b of the [slowquery-detective](https://pypi.org/project/slowquery-detective/) portfolio project. The PyPI middleware captures slow queries, but it needs a **live target** — a real service with real slow queries running on a public URL.

This backend is that target: a seeded commerce database with intentionally missing indexes, wired to the middleware, producing real fingerprints, EXPLAIN plans, and rule-engine suggestions that the [dashboard frontend](https://github.com/Abdul-Muizz1310/slowquery-dashboard-frontend) visualizes.

The demo's punchline: switch from the `slowquery` branch (no indexes, seq scans) to `slowquery-fast` (indexed) and the seq-scan / sort queries become index scans. The size of the p95 drop is **seed-dependent** — the full 1M-order seed produces a large, dramatic delta; the documented 100k-order seed shows a smaller one (see [DEVIATIONS §1 / §4](docs/DEVIATIONS.md)). Measure it yourself with [`benchmarks/bench_demo_latency.py`](benchmarks/bench_demo_latency.py) rather than trusting a number in a README.

---

## ✨ Features

- 🛒 Seeded commerce dataset — users, products, orders, order_items (100k+ rows)
- 🔀 Two Neon branches: `slowquery` (no indexes, seq scans) and `slowquery-fast` (4 indexes)
- 🔍 slowquery-detective middleware installed with 4 compatibility shims
- 📊 Dashboard API at `/_slowquery/*` — fingerprints, samples, plans, suggestions
- 📡 SSE stream for live p95 updates to the frontend
- 🔀 Branch switch endpoint — swap slow ↔ fast in one POST
- 🚦 Traffic generator script for burst testing
- 🧪 182 unit + 49 integration tests (Testcontainers), 85% unit-lane line coverage
- 🛡️ Pydantic v2 schemas, `Literal` types, frozen DTOs
- 🚀 Render free tier with auto-deploy via CI webhook

---

## 🏗️ System architecture

```mermaid
flowchart TD
    Client([HTTP Client / Dashboard]) --> Routers[api/routers<br/>users · products · orders · order_items · dashboard · branches]
    Routers --> Services[services<br/>user · product · order · store · branch_switcher]
    Services --> Repos[repositories<br/>per-entity async SQLAlchemy CRUD]
    Repos --> Neon[(Neon Postgres<br/>two branches)]

    subgraph Middleware
        MW[slowquery-detective<br/>+ 4 shims] -->|hooks| Queue[asyncio.Queue<br/>drainer task]
        Queue --> Bookkeeping[(fingerprints · samples<br/>plans · suggestions)]
    end

    Routers -.->|/_slowquery/*| Bookkeeping
    Routers -.->|SSE stream| Client
    Services --> MW
```

### 🔧 Shim architecture

```mermaid
flowchart LR
    Library[slowquery-detective<br/>v0.1.0 PyPI] --> S1[Shim 1<br/>add_event_handler → no-op]
    Library --> S2[Shim 2<br/>StoreWriter module swap<br/>→ PostgresStoreWriter]
    Library --> S3[Shim 3<br/>cursor.info → setattr<br/>on ExecutionContext]
    Library --> S4[Shim 4<br/>sync hook → async bridge<br/>asyncio.Queue + drainer]
    S4 --> EXPLAIN[Direct EXPLAIN<br/>real params, not synthesized]
    S4 --> Rules[run_rules<br/>rule engine]
```

### 🔀 Two-branch demo flow

```mermaid
flowchart LR
    subgraph Slow Branch
        SlowQ[SELECT … ORDER BY created_at] --> SeqScan[Seq Scan + Sort<br/>no index]
    end
    subgraph Fast Branch
        FastQ[Same query] --> IdxScan[Index Scan<br/>ix_orders_created_at]
    end
    Switch[POST /branches/switch<br/>target: fast] --> FastQ
```

> The exact cost / latency numbers depend on seed size and the Neon compute tier, so they are not asserted here. Run [`benchmarks/bench_demo_latency.py`](benchmarks/bench_demo_latency.py) against your own seeded branches to get real figures.

---

## 🗂️ Project structure

```
src/slowquery_demo/
├── main.py                        # FastAPI app factory, middleware, lifespan
├── api/
│   └── routers/
│       ├── users.py               # /users, /users/{id}/orders
│       ├── products.py            # /products
│       ├── orders.py              # /orders (fires sort_without_index rule)
│       ├── order_items.py         # /order_items
│       ├── dashboard.py           # /_slowquery/* (queries, detail, force-explain, SSE)
│       └── branches.py            # /branches/switch
├── services/
│   ├── user_service.py            # user query logic
│   ├── product_service.py         # product query logic
│   ├── order_service.py           # order / order-item query logic
│   ├── store.py                   # PostgresStoreWriter — shim 2 target
│   ├── store_errors.py            # StoreWriterError
│   └── branch_switcher.py         # branch-swap business logic
├── repositories/                  # per-entity async SQLAlchemy CRUD (sole SQLAlchemy importers)
│   ├── user_repository.py
│   ├── product_repository.py
│   ├── order_repository.py
│   ├── order_item_repository.py
│   └── slowquery_repository.py    # fingerprints, samples, plans, suggestions
├── models/                        # SQLAlchemy 2.0 async models
│   ├── user.py · product.py · order.py · order_item.py
│   └── slowquery_store.py         # 4 bookkeeping tables
├── schemas/                       # Pydantic v2 DTOs (frozen, Literal-typed)
│   └── user.py · product.py · order.py · pagination.py · branches.py · slowquery.py
└── core/
    ├── config.py                  # pydantic-settings from .env
    ├── database.py                # async engine + sessionmaker (pool_pre_ping, recycle)
    ├── db_config.py               # asyncpg URL normalisation
    ├── access.py                  # mutation cooldown + admin-token gates
    ├── platform.py                # CORS, /health, /version, /metrics, / → /docs
    ├── platform_token.py          # X-Platform-Token (async key fetch + claims)
    ├── errors.py                  # typed DomainError → HTTP mapping
    └── observability.py           # 4 library shims + drainer task

alembic/versions/0001_initial.py   # full 8-table schema (commerce + bookkeeping)
```

---

## 🌐 API surface

| Method | Endpoint | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness + service identity |
| `GET` | `/version` | Build version |
| `GET` | `/users?limit=N` | Seeded users (triggers slow query) |
| `GET` | `/users/{id}/orders` | Seq scan on `orders.user_id` (slow branch) |
| `GET` | `/orders?limit=N` | `ORDER BY created_at` — fires `sort_without_index` rule |
| `GET` | `/products?limit=N` | Seeded products |
| `GET` | `/_slowquery/queries` | Dashboard API — live fingerprint list with suggestions |
| `GET` | `/_slowquery/queries/{id}` | Full detail for one fingerprint (plan, samples, suggestions) |
| `POST` | `/_slowquery/queries/{id}/force-explain` | 🔒 Re-analyze (gated: requires `DEMO_MUTATION_TOKEN` even in demo mode) |
| `GET` | `/_slowquery/api/stream` | 📡 SSE — live p95 updates per fingerprint |
| `POST` | `/branches/switch` | Switch between `slow` and `fast` Neon branch (per-client cooldown) |
| `GET` | `/docs` | OpenAPI UI (`/` redirects here) |

> 🔒 The two state-mutating endpoints (`force-explain`, `branches/switch`) are hardened for the public URL: a per-client cooldown (`DEMO_MUTATION_COOLDOWN_S`) throttles both even under `DEMO_MODE`, and the destructive `force-explain` fails closed unless `DEMO_MUTATION_TOKEN` is set. See [DEVIATIONS](docs/DEVIATIONS.md).

---

## 🛠️ Stack

| Concern | Choice |
|---|---|
| **Language** | Python 3.12 (uv for everything, no pip) |
| **Framework** | FastAPI + SQLAlchemy 2.0 async + asyncpg |
| **Middleware** | [`slowquery-detective`](https://pypi.org/project/slowquery-detective/) v0.1.0 (with 4 in-repo compatibility shims) |
| **Database** | Neon serverless Postgres, two branches (`slowquery` / `slowquery-fast`) |
| **Migrations** | Alembic async env, one migration (`0001_initial.py`) — full 8-table schema |
| **Tests** | pytest + pytest-asyncio, 182 unit + 49 integration (Testcontainers), 85% unit-lane coverage |
| **Lint / Types** | ruff + mypy `--strict` on `src/` |
| **Hosting** | Render Free tier, auto-deploy via deploy-hook webhook from CI |
| **CI** | GitHub Actions: lint → test → build → deploy |

---

## 🚀 Run locally

```bash
# 1. clone & env
git clone https://github.com/Abdul-Muizz1310/slowquery-demo-backend.git
cd slowquery-demo-backend
uv sync --all-extras
cp .env.example .env
# fill in Neon + OpenRouter secrets

# 2. migrate & seed
DATABASE_URL=postgresql://.../neondb?sslmode=require \
  uv run alembic upgrade head
DATABASE_URL=postgresql://.../neondb?sslmode=require \
  uv run python -m scripts.seed_slow --reset \
    --users 1000 --products 200 --orders 100000 --order-items 500000

# 3. serve
uv run uvicorn slowquery_demo.main:app --reload
# → http://localhost:8000/health
# → http://localhost:8000/_slowquery/queries
```

### 🚦 Traffic generator

```bash
uv run python -m scripts.traffic_generator --burst 60
# 60-second burst → populates fingerprints, samples, plans, suggestions
```

---

## 🧪 Testing

```bash
uv run pytest                                     # full suite
uv run pytest -m "not slow and not integration"   # fast-only (CI)
uv run pytest --cov=src/slowquery_demo --cov-report=term-missing
```

| Metric | Value |
|---|---|
| **Unit tests** | 168 (unit lane, `-m "not slow and not integration"`) |
| **Integration tests** | 51 (Testcontainers) |
| **Line coverage** | **85%** (unit lane, `--cov-fail-under=80`) |
| **Methodology** | Red-first spec-TDD. Every spec in `docs/specs/` (`00`–`11`) enumerates its test cases before code ships. |

> **Honesty note:** every test now lives in a numbered `test_NN_*.py` file, each case carrying the `docs/specs/` case it verifies. The earlier standalone `*_coverage.py` files — an after-the-fact, line-targeting coverage pass that asserted mock-call counts and reached into private attributes — have been removed. The two largest (`test_full_coverage.py` + `test_final_coverage.py`, ~1.7k lines) and the six smaller ones (`{dashboard,pagination,repositories,router,services,store_writer}_coverage.py`, ~0.7k lines) were deleted; their behaviourally meaningful cases (typed-error wrapping in the store, `clamp_limit`/cursor round-trips, the fingerprint-id validator, repository/service DTO mapping) were folded into the matching numbered spec files, and the pure line-padding cases were dropped. The suite is now spec-first end to end.

---

## 📐 Engineering philosophy

| Principle | How it shows up |
|---|---|
| 🧪 **Spec-TDD** | Every feature slice has a spec in `docs/specs/` with enumerated test cases before code ships. |
| 🛡️ **Negative-space programming** | Typed domain errors (`UserNotFoundError`, `ConfigError`), `Literal` types for branch targets, frozen Pydantic DTOs. |
| 🏗️ **MVC layering** | `routers → services → repositories → models`. Controllers never touch SQLAlchemy; repos are the only layer that imports it. |
| 🔤 **Typed everything** | `mypy --strict` clean. Pydantic v2 DTOs, typed SQLAlchemy models, no `Any`. |
| 🌊 **Pure core, imperative shell** | Business logic stays unit-testable; DB/HTTP side effects live at the edges. |
| 🎯 **One responsibility per module** | Every file describes exactly one thing — never "and". |

---

## 🚀 Deploy

Render free tier via [`render.yaml`](render.yaml). CI fires a deploy-hook webhook on green builds.

1. Render dashboard → **New → Blueprint** → connect this repo
2. Fill env vars in service settings
3. Copy the Deploy Hook URL → `gh secret set RENDER_DEPLOY_HOOK --body '<url>'`
4. Push to `main` → CI lint/test/build → CI fires hook → Render rebuilds

---

## 📄 License

MIT. See [LICENSE](LICENSE).

---

> 🔬 **`slowquery-demo --help`** · seeded queries, real EXPLAIN plans, two branches
