# slowquery-demo-backend

> Live demo backend for [`slowquery-detective`](https://pypi.org/project/slowquery-detective/) — a FastAPI service seeded with a 1M-row dataset and wired to two Neon branches (`slowquery` and `slowquery-fast`) so the dashboard can flip between a missing-index nightmare and an indexed baseline in real time.

![ci](https://img.shields.io/github/actions/workflow/status/Abdul-Muizz1310/slowquery-demo-backend/ci.yml?style=flat-square)
![python](https://img.shields.io/badge/python-3.12-3776ab?style=flat-square&logo=python&logoColor=white)
![license](https://img.shields.io/github/license/Abdul-Muizz1310/slowquery-demo-backend?style=flat-square)

## What this repo is

This is **Phase 4b** of the slowquery-detective project.

- Phase 4a: the [slowquery-detective](https://pypi.org/project/slowquery-detective/) PyPI package (the middleware itself).
- **Phase 4b (this repo):** a feathers-generated FastAPI demo service that installs the middleware, seeds a realistic 1M-row schema with and without indexes, and exposes a `POST /branches/switch` endpoint that swaps between the two Neon branches live.
- Phase 4c: the [slowquery-dashboard-frontend](https://github.com/Abdul-Muizz1310/slowquery-dashboard-frontend) Next.js dashboard that reads the SSE stream this service exposes.

See [`docs/projects/50-slowquery-detective.md`](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/docs/projects/50-slowquery-detective.md) for the authoritative spec.

## Run locally

```bash
uv sync --all-extras
cp .env.example .env        # fill in Neon + OpenRouter secrets
uv run alembic upgrade head
uv run python scripts/seed_slow.py   # one-time — takes a while on a fresh branch
uv run uvicorn slowquery_demo.main:app --reload
# → http://localhost:8000/health
# → http://localhost:8000/_slowquery/queries (live detector API)
```

## Engineering notes

- Python 3.12, `uv` for everything, `ruff` + `mypy --strict` + `pytest-asyncio`.
- MVC layout: `api/routers` → `services` → `repositories` → `models` (SQLAlchemy async) → `schemas` (Pydantic DTOs).
- Four tables for the seeded commerce dataset (`users`, `products`, `orders`, `order_items`) plus four bookkeeping tables written by the slowquery-detective `StoreWriter` (`query_fingerprints`, `query_samples`, `explain_plans`, `suggestions`).
- Spec-TDD — every feature has an `docs/specs/<nn>-<slice>.md` with enumerated test cases before the red tests ship.
- Hosted on Render Free tier; the dashboard shows a "booting" UX when the service is sleeping.
