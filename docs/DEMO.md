# Demo script

This is what to click / curl to verify the pipeline end-to-end. No dashboard yet (Phase 4c), so everything in this document is either a `curl` against the live Render URL or a `psql` / asyncpg readback against the Neon `slowquery` branch.

## Prerequisites

- `curl`, `psql` (or any Postgres client), and `uv` installed locally
- Read-only access to the Neon `slowquery` branch connection string (in the workspace `.env` as `NEON_DB_URL_SLOWQUERY`)
- The live Render service: **https://slowquery-demo-backend.onrender.com**

## 60-second happy-path demo

### 1. Service is live

```bash
curl https://slowquery-demo-backend.onrender.com/health
# {"status":"ok","service":"slowquery_demo","version":"0.1.0","db":"unknown"}
```

First request after >15 minutes of idle will take ~30 seconds — that's Render Free tier cold-booting the container. Subsequent requests are fast.

### 2. The seeded data is real

```bash
curl 'https://slowquery-demo-backend.onrender.com/users?limit=3'
# {"items":[
#   {"id":"ff797b74-...", "email":"user_900@example.com", "full_name":"Ada Hopper", ...},
#   ...
# ]}
```

Three rows of the 1,000-user seeded dataset. The email pattern `user_<n>@example.com` is deterministic under seed=42.

### 3. The observability dashboard API responds

```bash
curl https://slowquery-demo-backend.onrender.com/_slowquery/queries
# []
```

Empty until traffic flows — that's the whole pipeline waiting for you.

### 4. Drive the traffic generator

```bash
cd slowquery-demo-backend
uv run python scripts/traffic_generator.py \
    --host https://slowquery-demo-backend.onrender.com \
    --duration 60 \
    --users 3 \
    --json
# {"total": 19, "failures": 1, "p95_ms": 41969.0, "exit_code": 1}
```

The generator is weighted toward the slow paths: `GET /users/{id}/orders` (25%), `GET /orders` (15%), `GET /orders/{id}` (20%), `GET /order_items?product_id=...` (15%), N+1 burst (5%), plus fast paths for `GET /users/{id}` and `GET /products/{id}`.

**Exit code 1 is expected on this run** — the p95 tripwire fires when any single request takes over 30 seconds, and the N+1 burst task's 50 serial calls from a dev laptop over the Render → Neon round-trip trip that threshold. The traffic still flows and the observability pipeline still fills. See [DEVIATIONS.md §2](DEVIATIONS.md) for the rationale.

### 5. Readback: verify the store actually got rows

```bash
psql "$NEON_DB_URL_SLOWQUERY" -c "
  SELECT
    (SELECT count(*) FROM query_fingerprints) AS fingerprints,
    (SELECT count(*) FROM query_samples)      AS samples,
    (SELECT count(*) FROM explain_plans)      AS plans,
    (SELECT count(*) FROM suggestions)        AS suggestions;
"
# fingerprints | samples | plans | suggestions
# -------------+---------+-------+-------------
#            7 |      22 |     7 |           5
```

Every row in those four tables came from a real endpoint call flowing through the slowquery-detective middleware → [shim 4 bridge queue](../src/slowquery_demo/core/observability.py) → async drainer → direct EXPLAIN → rules engine → store writer.

### 6. Inspect what the rules engine produced

```bash
psql "$NEON_DB_URL_SLOWQUERY" -c "
  SELECT fingerprint_id, kind, source,
         substring(rationale, 1, 60) AS rationale,
         substring(sql, 1, 60) AS suggestion
  FROM suggestions ORDER BY id;
"
# fingerprint_id   | kind  | source | rationale | suggestion
# -----------------+-------+--------+-----------+-----------
# c168fc78a2e7d01c | index | rules  | Sort node on ORDER BY created_at ... | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(created_at);
# 7f9c44d378fe11e2 | index | rules  | Sort node on ORDER BY created_at ... | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(created_at);
# ...
```

All rule-produced suggestions are runnable DDL. You can copy the `suggestion` column directly into a `psql` session and it applies the index.

### 7. Inspect a captured plan

```bash
psql "$NEON_DB_URL_SLOWQUERY" -c "
  SELECT fingerprint_id, cost, jsonb_pretty(plan_json)
  FROM explain_plans
  ORDER BY cost DESC
  LIMIT 1;
"
```

The most expensive plan will be a `Limit` over a `Sort` over a `Seq Scan` on the `orders` table — exactly the shape the `sort_without_index` rule matches against.

## What's deliberately missing from the happy path

Three things the spec promised that this repo's `v0.1.0` doesn't deliver. Each has a documented rationale and close path in [DEVIATIONS.md](DEVIATIONS.md):

1. **`seq_scan_large_table` and `n_plus_one` rules don't fire in the current configuration** ([DEVIATIONS.md §2](DEVIATIONS.md#2-rules-engine-1-of-3-rules-firing-on-live-traffic)).
2. **`POST /branches/switch` updates state but doesn't rebuild the SQLAlchemy engine at runtime** ([DEVIATIONS.md §3](DEVIATIONS.md#3-post-branchesswitch-does-not-actually-rebuild-the-engine)).
3. **LLM fallback is off by default** ([DEVIATIONS.md §5](DEVIATIONS.md#5-llm-fallback-llm_fallback_enabled-is-off-in-production)).

## When the dashboard lands (Phase 4c)

The data in `query_fingerprints`, `query_samples`, `explain_plans`, `suggestions` is everything the dashboard needs to render:

- `query_fingerprints` → the main table with one row per pattern, live p50/p95/p99
- `query_samples` → the Recharts timeline (sparkline per fingerprint)
- `explain_plans` → the detail drawer with the plan rendered via Monaco + postgres-plan highlighter
- `suggestions` → the "Apply on fast branch" button card

Until then, the demo is the seven readback commands above.
