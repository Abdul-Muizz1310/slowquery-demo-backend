# Demo script

This is what to click / curl to verify the pipeline end-to-end: a `curl` against the live Render URL, the service's own read API at `/_slowquery/*`, or a `psql` / asyncpg readback against the Neon `slowquery` branch.

The dashboard read API and the SSE stream **have shipped** — `GET /_slowquery/queries`, `GET /_slowquery/queries/{id}`, `GET /_slowquery/api/stream` — so the readbacks below are a cross-check on the store rather than the only way to see the data. Every command in this file was run against the live service; the numbers are a 2026-08-02 snapshot and will have moved by the time you read them.

## Prerequisites

- `curl`, `psql` (or any Postgres client), and `uv` installed locally
- Read-only access to the Neon `slowquery` branch connection string (in the workspace `.env` as `NEON_DB_URL_SLOWQUERY`)
- The live Render service: **https://slowquery-demo-backend.onrender.com**

## 60-second happy-path demo

### 1. Service is live

```bash
curl -s https://slowquery-demo-backend.onrender.com/health
# {"status":"ok","service":"slowquery_demo","version":"0.1.0","commit_sha":"324888bc01f4efc1d29bfff4c0a3d9be889af350","db":"ok"}
```

`db` is the result of a real `SELECT 1` against `app.state.engine`, so it is `"ok"` (HTTP 200) or `"down"` (HTTP 503) — never `"unknown"`. `commit_sha` comes from Render's `RENDER_GIT_COMMIT`, so you can confirm the running container matches `git rev-parse HEAD`.

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
curl -s https://slowquery-demo-backend.onrender.com/_slowquery/queries \
  | jq -r '.[:5][] | "\(.id)  calls=\(.call_count)  p95=\(.p95_ms)  suggestions=\(.suggestions|length)"'
# ab4308f592d9b528  calls=641   p95=10.0257072597742  suggestions=0
# 05306816406982a5  calls=145   p95=14.7220706101507  suggestions=0
# 7f9c44d378fe11e2  calls=211   p95=10.6789851561189  suggestions=3
# 7ae509fc5e11f3bd  calls=2364  p95=6.06597745791078  suggestions=0
# fb61f8583a991b0f  calls=19    p95=682.215396687388  suggestions=0
```

21 fingerprints at the time of that snapshot. On a freshly-seeded branch this is `[]` until traffic flows — that's the pipeline waiting for you.

**Read the rows above critically.** Four of those five are the observability system's *own* bookkeeping reads (`select … from query_fingerprints`, `… from suggestions`) and `/health`'s `SELECT 1`, captured before the self-instrumentation ignore list existed. `observability.should_ignore_statement` (spec 05 invariants 5-6) now filters them, so nothing new like that is recorded — but the historical rows persist in `query_fingerprints` until the table is cleared:

```sql
TRUNCATE query_fingerprints CASCADE;  -- cascades to samples / plans / suggestions
```

The one that matters is `7f9c44d378fe11e2` — `GET /users/{id}/orders` on the slow branch, with 3 rule-produced suggestions. Its detail view:

```bash
curl -s https://slowquery-demo-backend.onrender.com/_slowquery/queries/7f9c44d378fe11e2 | jq
```

### 3b. The SSE stream is live

```bash
curl -N -s https://slowquery-demo-backend.onrender.com/_slowquery/api/stream | head -5
```

Emits a `tick` batch immediately (one per fingerprint with a computed p95), then a `tick` whenever a p95 moves and a `heartbeat` every ~2 s when nothing changed. This is what the dashboard's live timeline consumes.

### 3c. Which branch is active

```bash
curl -s http://localhost:8000/branches/current
# {"active":"slow"}
```

Read-only and ungated (no cooldown, no admin token), so a dashboard can poll it. Its mutating counterpart is `POST /branches/switch`.

> The output above was measured against a locally-built app. `GET /branches/current` is **newer than the currently-deployed container** (`commit_sha` `324888b`), where the same path still returns `404`. It will answer on the live URL after the next deploy.

### 4. Drive the traffic generator

```bash
cd slowquery-demo-backend
uv run python scripts/traffic_generator.py \
    --host https://slowquery-demo-backend.onrender.com \
    --duration 60 \
    --users 3 \
    --json
# {"total": 36, "failures": 0, "p95_ms": 14062.000000005355, "exit_code": 0}
```

The generator is weighted toward the slow paths: `GET /users/{id}/orders` (25%), `GET /orders` (15%), `GET /orders/{id}` (20%), `GET /order_items?product_id=...` (15%), N+1 burst (5%), plus fast paths for `GET /users/{id}` and `GET /products/{id}`.

**The exit code is a tripwire, not a pass/fail on the demo.** It is `1` when p95 crosses 30 000 ms or the failure rate crosses 20 %. Whether it fires depends on whether the N+1 burst task (50 serial `GET /users/{id}/orders` calls over the laptop → Render → Neon round-trip) got picked inside the window and how warm the container was: an earlier documented run on a cold container produced `{"total": 19, "failures": 1, "p95_ms": 41969.0, "exit_code": 1}`, the run above on a warm one produced `exit_code: 0`. Traffic flows and the observability pipeline fills either way. See [DEVIATIONS.md §2](DEVIATIONS.md) for why the N+1 rule still does not fire.

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
#           21 |     837 |    20 |           5
```

Every row in those four tables came from a real endpoint call flowing through the slowquery-detective middleware → [shim 4 bridge queue](../src/slowquery_demo/core/observability.py) → async drainer → direct EXPLAIN → rules engine → store writer. (`query_samples` is pruned to `SLOWQUERY_SAMPLE_RETENTION_S`, 1 day by default, so `samples` reflects roughly the last day of traffic rather than all time.)

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
# c168fc78a2e7d01c | index | rules  | Sort node on ORDER BY created_at with significant cost; an i | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(cr
# 7f9c44d378fe11e2 | index | rules  | Sort node on ORDER BY created_at with significant cost; an i | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(cr
# 7f9c44d378fe11e2 | index | rules  | Sort node on ORDER BY created_at with significant cost; an i | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(cr
# c168fc78a2e7d01c | index | rules  | Sort node on ORDER BY created_at with significant cost; an i | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(cr
# 7f9c44d378fe11e2 | index | rules  | Sort node on ORDER BY created_at with significant cost; an i | CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(cr
```

All five rows, verbatim — every rule-produced suggestion is runnable DDL, so you can copy the `suggestion` column straight into a `psql` session and it applies the index. Only `sort_without_index` fires today, and it fires repeatedly for the same fingerprint (one row per EXPLAIN outside the cooldown), which is why two fingerprints account for all five rows; [DEVIATIONS §2](DEVIATIONS.md) explains why the other two rules stay quiet at this seed size.

### 7. Inspect a captured plan

```bash
psql "$NEON_DB_URL_SLOWQUERY" -c "
  SELECT fingerprint_id, cost, jsonb_pretty(plan_json)
  FROM explain_plans
  ORDER BY cost DESC
  LIMIT 1;
"
```

Three most expensive captured plans by `cost` at the time of this snapshot: `63b39109c87bef9d` (9332.77), `c168fc78a2e7d01c` (3613.87), `7f9c44d378fe11e2` (2298.91). The expensive ones are a `Limit` over a `Sort` over a `Seq Scan` on `orders` — exactly the shape the `sort_without_index` rule matches against.

## What's deliberately missing from the happy path

Three things the spec promised that this repo's `v0.1.0` doesn't deliver. Each has a documented rationale and close path in [DEVIATIONS.md](DEVIATIONS.md):

1. **`seq_scan_large_table` and `n_plus_one` rules don't fire in the current configuration** ([DEVIATIONS.md §2](DEVIATIONS.md#2-rules-engine-1-of-3-rules-firing-on-live-traffic)).
2. **`POST /branches/switch` rebuilds the commerce engine, clears the buffer, and re-attaches the in-process listeners — but the drainer's EXPLAIN pool does not yet repoint at the new branch** ([DEVIATIONS.md §3](DEVIATIONS.md#3-post-branchesswitch-rebuilds-the-commerce-engine-but-the-observability-explain-pool-does-not-follow-the-swap)).
3. **LLM fallback is off by default** ([DEVIATIONS.md §5](DEVIATIONS.md#5-llm-fallback-llm_fallback_enabled-is-off-in-production)).

## What the dashboard frontend consumes

The read API in step 3 is what the [dashboard frontend](https://github.com/Abdul-Muizz1310/slowquery-dashboard-frontend) renders from — it never touches Postgres directly:

| Endpoint | Backed by | Renders |
|---|---|---|
| `GET /_slowquery/queries` | `query_fingerprints` + `suggestions` | the main table, one row per pattern, live p50/p95/p99 |
| `GET /_slowquery/queries/{id}` | `+ explain_plans` + `query_samples` | the detail drawer: plan tree, recent samples, suggestion cards |
| `GET /_slowquery/api/stream` | `query_fingerprints` (polled) | the live timeline — `tick` on a p95 change, `heartbeat` otherwise |
| `POST /branches/switch` | `BranchSwitcher` | the "apply index on fast branch" button |
| `GET /branches/current` | `BranchSwitcher.active` | the slow/fast state badge |

The response DTOs live in [`schemas/slowquery.py`](../src/slowquery_demo/schemas/slowquery.py) and are mirrored by Zod schemas on the frontend, so a contract drift surfaces as a parse error rather than a silent type hole.

The `psql` readbacks in steps 5-7 remain useful for a different reason: they prove the rows the API returns really came out of the pipeline, rather than from a stub in the handler.
