# Spec 15 — Move onto slowquery-detective 0.2

## Goal

slowquery-detective 0.2.0 upstreamed the four shims this demo carried in `core/observability.py`
(DEVIATIONS §8): lifespan instead of `add_event_handler`, an injectable Postgres store, per-execution
timing on the `ExecutionContext`, and `EXPLAIN` with the statement's real parameters. It also adds request
tagging (`X-Request-Id` → which queries did this request cause), which bastion's dossier "measure" stage
reads from every service at `GET /_slowquery/queries?request_id=`. This spec replaces the shims, the
demo's own store writer, its bookkeeping repository and its two read routes with the library's public API.
Library contract: `slowquery-detective/docs/specs/07-fleet-observability.md` (interface I-2).

## Wiring (`core/observability.py`, `main.py`)

```python
install(app, engine, service="slowquery",
        threshold_ms=SLOWQUERY_THRESHOLD_MS, sample_rate=SLOWQUERY_SAMPLE_RATE,
        store_url=SLOWQUERY_STORE_URL or DATABASE_URL,
        authorize=platform_admitted, mount_path="/_slowquery",
        enable_llm=LLM_FALLBACK_ENABLED, llm_config=<from OPENROUTER_*>)
```

- `install` runs after the host's own middleware, so the library's request tagger is the outermost layer.
- The library owns `GET /_slowquery/queries` (`?request_id=&since=&limit=`) and
  `GET /_slowquery/queries/{id}`. The demo keeps its own `GET /_slowquery/api/stream` (SSE, spec 09) and
  `POST /_slowquery/queries/{id}/force-explain` (spec 11); the library removed both kinds of route.
- `authorize = platform_admitted`: `True` iff the contract-v2 middleware (spec 14) admitted the request,
  i.e. it carried a valid bastion or slowquery-frontend token, or it carried none and `DEMO_MODE=true`
  let it through anonymous. The middleware records that on `request.state.platform_admitted`; a request
  that reached the route any other way gets 403. So the library's routes follow the same contract as every
  other protected route, `DEMO_MODE` included.
- LLM: `LLM_FALLBACK_ENABLED=true` without `OPENROUTER_API_KEY` fails at boot (`ConfigError`); the model
  slots default to the portfolio's free models (`google/gemma-4-26b-a4b-it:free`,
  `xingchen-agi/xing4.0-29b-a4b:free` since minimax-m3.1-flash left OpenRouter on 2026-10-26,
  `google/gemma-4-31b-it:free`).

## One observed engine, repointed on a branch switch (`core/database.py`, `main.py`; replaces spec 10's rebuild)

The library binds its hooks and its `EXPLAIN` worker to the engine passed to `install`, for the app's
lifetime. A branch switch therefore no longer builds a new engine. Instead:

- `BranchTarget` holds the active branch's URL; the commerce engine connects through an `async_creator`
  that dials `BranchTarget.url`. It is created once, instrumented once.
- `POST /branches/switch` health-checks the target first (a raw connection and `SELECT 1`, 5 s), then sets
  `BranchTarget.url` and disposes the engines' pools, so every new connection goes to the target branch.
  In-flight queries finish on the connections they hold. A failed check raises before anything changes.
- After the switch the library's ring buffer is cleared (spec 06 invariant 5: the rolling percentiles
  restart on the new branch).
- `EXPLAIN` now follows the switch too (it runs on the same engine), which closes the remaining gap of
  DEVIATIONS §3.
- The engine's initial target is the persisted active branch (`.branch_state`), so a restart on `fast`
  serves `fast`.
- `/health` probes through a second engine on the same `BranchTarget` that is **never instrumented**, so the
  probe's `SELECT 1` is never a fingerprint (spec 05's self-instrumentation guarantee, now structural).
  The store's own reads and writes run on the library's private engine, which is never instrumented either.

## Data: a clean start in schema `slowquery`

The library keeps its tables in schema `slowquery`, created at startup with `CREATE … IF NOT EXISTS` under
an advisory lock (safe on every boot and on concurrent boots), on the store database only
(`SLOWQUERY_STORE_URL`, else `DATABASE_URL`: the slow branch). Its tables differ from the demo's (traffic is
keyed by `(service, id)`, plans are stored scrubbed of literal values), and the demo's old plans were
captured with real parameters, so they cannot be copied as they are.

**Decision: clean start.** Nothing is copied. The scheduled traffic workflow (spec 07) repopulates
fingerprints, percentiles, plans and suggestions within one run. The demo's own bookkeeping tables in
schema `public` (created by `0001`/`0002`) are left in place, unused and unwritten: no migration drops
them, so rolling back to the pre-0.2 release still finds its tables and its alembic head. A later
migration may drop them once 0.2 has settled. The container entrypoint is unchanged: it migrates the
commerce schema on both branches (spec 13); the library creates its own schema at runtime on the store
database only.

## Response shapes

The library's models differ from what the demo served (`schemas/slowquery.py`, now removed):

| Field | Demo (0.1 era) | Library 0.2 |
|---|---|---|
| suggestion | `id, fingerprint_id, kind, source, rule, sql, rationale, applied_at` | `kind, source, sql, rationale, applied_at` |
| fingerprint `total_ms`, `max_ms` | integer / number | number (float) |
| `explain_plan` | `fingerprint_id, plan_json, plan_text, cost, captured_at` | `plan_json, cost, captured_at` |
| `recent_samples[]` | `id, fingerprint_id, params, duration_ms, rows, sampled_at` | `duration_ms, request_id, sampled_at` |
| `GET /queries` | every fingerprint | `limit` 1–500, default 50 |

The routes are the library's (bastion reads `?request_id=` from them), so the dashboard adapts on its
`contract-v2` branch: its schemas accept both shapes.

## Test cases

**Unit**

1. `create_app()` installs the library once: `app.state.slowquery_installed`, a `PostgresStoreWriter` whose
   URL is `SLOWQUERY_STORE_URL` when set, else `DATABASE_URL`; the runtime's service is `"slowquery"`.
2. The library's two read routes and the demo's stream and force-explain routes are mounted under
   `/_slowquery`; there is no top-level `/queries` and no 0.1 route (`/_slowquery/api/queries`).
3. `LLM_FALLBACK_ENABLED=false` ⇒ no LLM; `true` without a key ⇒ `ConfigError` at boot; `true` with a key ⇒
   the key and the models reach `install`; the model defaults are the three free models.
4. `platform_admitted`: `True` only when `request.state.platform_admitted is True`; missing state ⇒ `False`.
5. Composed, production settings: `GET /_slowquery/queries` with no token ⇒ 401 (middleware); with a valid
   bastion or frontend token ⇒ 200; with `DEMO_MODE=true` and no token ⇒ 200; through `create_app()` with the
   middleware bypassed (state never set) the library answers 403.
6. The commerce engine is instrumented; the health engine is not; both connect to `BranchTarget.url`.
6b. The connector dials the target's current URL as an asyncpg DSN (libpq `sslmode` kept,
    `channel_binding` dropped, the `+asyncpg` suffix removed); the reachability check closes its
    connection whether or not `SELECT 1` succeeds; a `BranchTarget` needs a URL; alembic's URL is normalised
    for SQLAlchemy's asyncpg dialect.
7. Switch: the target is health-checked before the URL changes; on success the URL is the target's, both
   pools are disposed, the engine object is unchanged and still instrumented, the ring buffer is cleared;
   on a failed check nothing changes and the switch raises.
8. The initial target is the persisted branch's URL.
9. SSE: the stream reads p95 from the library store (`list_queries`); a store that cannot start yields a
   heartbeat, not an error.
10. force-explain: unknown fingerprint ⇒ 404; store unavailable ⇒ 503; known ⇒ the synthetic plan is stored
    with `upsert_plan`, the rules run on the fingerprint's canonical SQL, the LLM is asked only when they
    find nothing and LLM is enabled, and suggestions go through `insert_suggestions`. The admin-token gate
    and cooldown (spec 11) are unchanged.

**Integration (Testcontainers Postgres)**

11. WP-8 acceptance: `GET /users` with `X-Request-Id: R` ⇒ `GET /_slowquery/queries?request_id=R` lists the
    fingerprints that request ran with its call counts; a request without the header adds nothing to R.
12. Live traffic through uvicorn populates `slowquery.query_fingerprints` (spec 07 cases 4–7 keep passing).
13. A slow `ORDER BY created_at` on the slow branch is EXPLAINed by the library (threshold 1 ms) and its
    stored plan shows the Seq Scan the rules key on; any suggestion is an index. (Whether
    `sort_without_index` fires depends on the seed size, DEVIATIONS §2.) On the fast branch no suggestion
    appears.
14. After a switch to `fast`, queries and `EXPLAIN` run on the fast database; fingerprints keep recording.
15. `/health` calls never create a fingerprint.
16. Startup creates schema `slowquery`; the `public` bookkeeping tables and their rows are untouched;
    a second boot over the same database succeeds.
17. force-explain with the LLM enabled and rules missing calls OpenRouter once (mocked) and stores its
    suggestion.
