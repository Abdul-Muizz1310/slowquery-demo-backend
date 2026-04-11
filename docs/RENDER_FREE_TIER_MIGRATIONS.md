# Running alembic migrations on Render Free

## The problem

`render.yaml` in this repo declares:

```yaml
services:
  - type: web
    plan: free
    preDeployCommand: alembic upgrade head
```

That **silently does nothing** on the Free plan. Render's
[own docs](https://render.com/docs/deploys#pre-deploy-command) gate
`preDeployCommand` behind paid tiers. On Free the field is accepted
by the YAML parser, the dashboard shows it as "configured", and
Render's deploy pipeline ignores it.

The first real sign of trouble is a 500 from the first endpoint
that touches SQLAlchemy (in this repo, `GET /users`):

```
curl https://slowquery-demo-backend.onrender.com/users?limit=1
HTTP 500 Internal Server Error
```

while `GET /health`, `GET /version`, and `GET /_slowquery/queries` all
return 200 because none of them hit the database.

## Current workaround (as of 2026-04-12)

One-time manual migration from a dev machine, executed against the
live Neon branches:

```bash
cd slowquery-demo-backend

DATABASE_URL='postgresql+asyncpg://.../neondb?sslmode=require&channel_binding=require' \
  uv run alembic upgrade head

DATABASE_URL='postgresql+asyncpg://.../neondb?sslmode=require&channel_binding=require' \
  uv run alembic upgrade head   # against DATABASE_URL_FAST's host
```

`slowquery_demo.core.db_config.normalise_asyncpg_url()` strips the libpq
`sslmode` / `channel_binding` params that asyncpg doesn't understand,
so the raw Neon URL works without hand-editing.

This unblocks the first deploy and subsequent schema revisions can be
applied from the same dev machine before merging the revision to main.
It is the right choice **for this repo right now** because:

- Schema changes are expected to be rare (the demo is a portfolio
  piece; the spec has one migration and no planned follow-ups).
- Neon branches are single-developer; there's no second deployer who
  might race the manual step.
- Every migration already requires a human review anyway, and
  that human runs the upgrade in the same sitting.

## Durable fix — run alembic inside the container

When schema churn picks up or a second deployer joins, the manual step
becomes a footgun. The durable fix is to run `alembic upgrade head` as
part of the container's start command so every Render redeploy applies
pending migrations automatically.

Two mechanical options, both Free-tier compatible.

### Option B1 — Dockerfile CMD

Change the last line of `Dockerfile` from

```dockerfile
CMD ["uvicorn", "slowquery_demo.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

to

```dockerfile
CMD ["sh", "-c", "alembic upgrade head && uvicorn slowquery_demo.main:app --host 0.0.0.0 --port $PORT"]
```

Pros: source of truth is the Dockerfile; `docker run` locally behaves
identically to Render; no config spread across two files.

Cons: every Free-tier cold start (after ~15 min idle) re-runs
`alembic upgrade head`. That's a no-op when already at head but adds
roughly 2 seconds to wake-up latency on top of Render's usual
~30-second cold boot.

### Option B2 — `startCommand` in `render.yaml`

Add to the `services[0]` entry:

```yaml
startCommand: sh -c "alembic upgrade head && uvicorn slowquery_demo.main:app --host 0.0.0.0 --port $PORT"
```

Keep the Dockerfile `CMD` as it is.

Pros: Render-specific; doesn't affect local `docker run`; easy to
diff in a PR.

Cons: second source of truth for startup; surprising if someone
reads the Dockerfile and assumes it represents reality on Render.

### Recommended: B1 + explicit idempotence check

Fold the alembic step into the Dockerfile so local and Render behave
identically, but wrap it in a guard that exits fast when already at
head. Rough shape:

```dockerfile
CMD ["sh", "-c", "alembic current 2>/dev/null | grep -q 'head' || alembic upgrade head; exec uvicorn slowquery_demo.main:app --host 0.0.0.0 --port $PORT"]
```

This keeps cold-boot overhead off the happy path when the DB is
already migrated, and still runs the upgrade on the first boot after
a new migration ships.

## Trade-offs

| Concern | Current (manual) | B1 Dockerfile CMD | B2 render.yaml start |
|---|---|---|---|
| Works on Render Free | yes | yes | yes |
| Migration on every deploy automatic | **no** | yes | yes |
| Cold-start latency | 0s overhead | +2s (no-op) | +2s (no-op) |
| Local `docker run` matches Render | yes | yes | **no** |
| Single source of truth | n/a | Dockerfile | Dockerfile + render.yaml |
| Safe under concurrent deploys | **no** — race window | yes | yes |

## When to migrate off the current workaround

Move to B1 the first time any of these is true:

- A second person deploys this repo.
- A migration ships in a normal merge commit rather than a dedicated
  release session (i.e. no human runs alembic by hand at merge time).
- The CI `deploy` job starts firing the Render hook automatically on
  every push to main (see `RENDER_DEPLOY_HOOK` / `ci.yml`).

Until then, the manual step is the least-moving-parts option.

## Related

- [`render.yaml`](../render.yaml) — currently declares a
  `preDeployCommand` that is silently ignored on Free.
- [`src/slowquery_demo/core/db_config.py`](../src/slowquery_demo/core/db_config.py) — holds `normalise_asyncpg_url` so the raw Neon URL works.
- [`alembic/env.py`](../alembic/env.py) — async env wired to
  `slowquery_demo.models.Base.metadata`.
