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

## 3. EXPLAIN did not follow a branch switch (closed 2026-10-20, spec 15)

**Spec says:** "Branch switch toggles between slow and fast demos".

**Used to ship:** `POST /branches/switch` rebuilt the commerce `AsyncEngine` and re-attached the hooks to it, but the drainer's direct `EXPLAIN` ran through the bookkeeping store's pool (the slow branch), so after a switch to `fast` the captured plans and suggestions still described the slow branch.

**Repo ships (now):** slowquery-detective 0.2 binds its hooks and its `EXPLAIN` worker to the engine handed to `install` for the app's lifetime, so the switch no longer replaces that engine. One commerce engine connects through an `async_creator` that dials `BranchTarget.url`; a switch health-checks the target (`SELECT 1`, 5 s), sets the URL and disposes the pools. Queries, the library's hooks and its `EXPLAIN` all follow the switch. The library's ring buffer is cleared after it (spec 06 invariant 5). Persisted fingerprints, plans and suggestions live in the library's store on the slow branch (`SLOWQUERY_STORE_URL`, else `DATABASE_URL`), independent of the switch, which is what the dashboard reads. Verified in the integration lane against two Postgres containers.

## 4. `SLOWQUERY_THRESHOLD_MS` default is too high for the current data size

**Spec says:** threshold of 100 ms is "any query exceeding the p95 threshold".
**Repo ships:** threshold defaults to 100 ms and queries against the seeded 100k-row dataset often complete under that threshold on Neon, so the drainer's EXPLAIN + rules path runs less often than it should.

**Why:** coupled to deviation 1. The 1200 ms → 18 ms story in the spec assumes data large enough to make sequential scans expensive; at 100k rows the Bitmap Heap Scan path is already cheap.

**To close:** either scale the seed or lower the threshold in Render's env to 10 ms. Both are one-line changes. I've chosen to ship at the spec-default threshold for now and document the gap honestly.

## 5. LLM fallback (`LLM_FALLBACK_ENABLED`) is off in production

**Spec says:** "When the rules don't match, an LLM is asked 'explain why this plan is slow'".
**Repo ships:** `LLM_FALLBACK_ENABLED=false` — both in this doc **and** in [`render.yaml`](../render.yaml) (previously the blueprint set `"true"`, which contradicted this section and, because `_build_llm_config` raises `ConfigError` at boot when `OPENROUTER_API_KEY` is unset, was a live boot footgun). The two now agree: the flag is off. The code path is wired (`core/observability._build_llm_config`, library passes the `LlmConfig` through), but the flag is off.

**Why:** the OpenRouter free-tier slug in the [memory pick](https://pypi.org/project/slowquery-detective/) is `nvidia/nemotron-nano-9b-v2:free` and I wanted to validate the rules-only path first before adding an LLM round-trip to the drainer's critical section. The LLM cascade (PRIMARY → FAST → FALLBACK) is well-tested in the library's own unit suite, but I haven't yet observed it live against real Neon data.

**To close:** provision `OPENROUTER_API_KEY` (and `OPENROUTER_MODEL_PRIMARY`) in the Render dashboard **first**, then flip `LLM_FALLBACK_ENABLED=true` in `render.yaml` / Render env — enabling it without the key set will make the service fail fast at boot. If rules fire on a plan the drainer will skip the LLM entirely; if they don't, the drainer will call the library's `explain()` function and persist any resulting suggestion alongside rule-produced ones.

## 6. Migrations: `preDeployCommand` was ignored, and an applied migration was edited (closed 2026-10-20)

**Spec says (implicitly):** Render runs `alembic upgrade head` as a pre-deploy step.
**What happened:** `preDeployCommand` is gated behind Render's paid plans; on Free the field is parsed and never executed, so `0001` was applied to both Neon branches by hand on 2026-04-12. The next day `0001_initial.py` was *edited* to add `uq_suggestions_fp_kind_sql`, and the store writer's insert became `ON CONFLICT (fingerprint_id, kind, sql) DO NOTHING` in the same commit. Alembic never re-runs a recorded revision, so the live branches never got the constraint: duplicate suggestions accumulated (one fingerprint listed `ix_orders_created_at` three times), and every later suggestion insert failed, because `ON CONFLICT (cols)` needs a matching unique constraint. Fresh Testcontainers databases got the constraint from the edited file, so no test ever saw either symptom.

**Repo ships (now):** [spec 13](specs/13-migrations-on-deploy.md).

- `alembic/versions/0002_dedupe_suggestions.py` locks `suggestions`, deletes every duplicate except the lowest `id` of each `(fingerprint_id, kind, sql)` group, and adds the constraint only where it is missing — correct on the live branches (no constraint) and on fresh databases (constraint from the edited `0001`). `0001` is pinned by hash in a unit test so it cannot be edited again.
- [`docker-entrypoint.sh`](../docker-entrypoint.sh) runs [`scripts/migrate_all.py`](../scripts/migrate_all.py) before uvicorn: `alembic upgrade head` against `DATABASE_URL`, `DATABASE_URL_FAST` and `SLOWQUERY_STORE_URL` (each distinct database once). A failure stops the boot, so Render keeps the previous deploy serving. `render.yaml` no longer declares `preDeployCommand`.
- Verified against real Postgres in the integration lane (fresh and live-shaped databases, re-runs, downgrade, both branches in one run) and, in CI's `build` job, by booting the built image against two empty Postgres services.

**Remaining cost:** every cold boot pays for one `alembic upgrade head` per database (a no-op at head, about a second each over Neon). [`RENDER_FREE_TIER_MIGRATIONS.md`](RENDER_FREE_TIER_MIGRATIONS.md) keeps the original option analysis.

## 7. Integration lane runs in CI (closed 2026-10-20)

**Spec implies:** full test matrix runs in CI.
**Used to ship:** the integration tests under `tests/integration/` were committed but filtered out of every CI run ("Docker Desktop wasn't running on my dev laptop").

**Repo ships (now):** [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) has an `integration` job that runs `uv run pytest -m integration` on the GitHub runner's Docker, so every push and pull request runs the Testcontainers lane (two `postgres:16-alpine` containers: the slow and the fast branch). The unit lane and its coverage floors are unchanged.

## 8. Four library bugs fixed by in-repo shims rather than upstream (closed 2026-10-20, spec 15)

**Used to ship:** `core/observability.py` carried four monkey-patches over slowquery-detective 0.1.0 (lifespan instead of `add_event_handler`, an injectable store, timing state on the `ExecutionContext` instead of `cursor.info`, a sync-hook-to-async-store bridge with `EXPLAIN` on real parameters), plus the demo's own `PostgresStoreWriter`, bookkeeping repository and read routes. The dependency was pinned to `==0.1.0` because any release could break the patches.

**Repo ships (now):** slowquery-detective 0.2.0 upstreamed all four (its `docs/specs/07-fleet-observability.md`). The demo depends on `slowquery-detective[fastapi,postgres]>=0.2,<0.3` and calls only `install(app, engine, service="slowquery", store_url=…, authorize=…, mount_path="/_slowquery")`; the shims, the store writer, the repository and the 0.1-era read routes are deleted. The library owns `GET /_slowquery/queries` (with `?request_id=` for bastion's measure stage) and `GET /_slowquery/queries/{id}`; the demo keeps its SSE stream and force-explain on top of the library's store. Data starts clean in schema `slowquery`; the old `public` bookkeeping tables stay, unused (spec 15 explains why).

## 9. State-mutating endpoints are hardened for the public URL

**Context (before contract v2):** `DEMO_MODE=true` bypassed the platform-token middleware for every route, so on the live URL the two endpoints with lasting cross-visitor side effects were unauthenticated. `POST /_slowquery/queries/{id}/force-explain` could overwrite a genuine captured EXPLAIN plan with a synthetic stub (permanent corruption), and `POST /branches/switch` could be spammed into repeated engine-rebuild churn against free-tier Neon/Render.

**Repo ships (now):** both are gated in [`core/access.py`](../src/slowquery_demo/core/access.py), enforced *even under `DEMO_MODE`*:

- A per-client cooldown (`DEMO_MUTATION_COOLDOWN_S`, seconds; `0` disables) throttles both endpoints and returns `429` inside the window.
- `force-explain` (destructive) is **fail-closed**: with no `DEMO_MUTATION_TOKEN` configured it returns `403`, so an anonymous visitor can never clobber real data. With a token set it requires a matching `X-Admin-Token` header (constant-time compare).
- `branches/switch` (transient churn, bounded by the cooldown) stays public by default so the demo punchline works, but is locked to the same admin token the moment `DEMO_MUTATION_TOKEN` is set.

`render.yaml` ships `DEMO_MUTATION_COOLDOWN_S=3`; `DEMO_MUTATION_TOKEN` is left unset (`sync: false`) so `force-explain` is disabled until an operator provisions it in the Render dashboard.

**Note vs. the audit's wording:** the fix is implemented as route-level dependencies rather than special-casing paths inside the token middleware — it achieves the same "require a secret for mutation even in demo mode" intent while keeping the middleware single-purpose.

## 10. Traffic generator is httpx-based, not a Locust file run by a Render cron (spec rewritten 2026-10-20)

**Spec used to say:** the deliverable was "a Locust file (`from locust import HttpUser, task, between`) plus a thin `__main__` that runs it headless", run by "a Render cron worker configured in `render.yaml` (`type: cron`, schedule `0 * * * *`)", with exit codes that would surface "as a red cron run in Render".

**Repo ships:** [`scripts/traffic_generator.py`](../scripts/traffic_generator.py) is a single-file **httpx** driver (`--host`, `--duration`, `--users`, `--json`; no `locust` dependency, no `TRAFFIC_*` env vars), and there was never a Render cron service. For months nothing generated traffic on a schedule at all: the live dashboard's newest sample was from 2026-08-02. [`.github/workflows/traffic.yml`](../.github/workflows/traffic.yml) now runs the driver every six hours (and on demand) against the live URL, after waking the free-tier service with spec 12's `/health` poller; the driver's exit code makes a broken demo a red Actions run.

**Why httpx:** the generator is ~200 lines of `asyncio` + `httpx`, and `httpx` is already a runtime dependency. Locust would add a heavyweight dependency, a gevent worker model that fights `asyncio`, and a web UI whose only role would be to be switched off. Why GitHub Actions instead of a Render cron: a Render cron job is a separately billed service, and the workflow needs no secret because the commerce routes it calls are public.

**Closed by:** [spec 07](specs/07-traffic-generator.md) was rewritten around what ships (driver, task mix, `--json` shape, exit-code contract, the schedule) and the Locust language dropped. Test numbers 1–14 kept their meaning; tests 15–18 pin the workflow.

## 11. There is no CI deploy job — Render auto-deploys `main` itself

**Docs used to say:** "Render free tier with auto-deploy via CI webhook", "auto-deploy via deploy-hook webhook from CI", a CI graph of "lint → test → build → deploy", and a setup step reading `gh secret set RENDER_DEPLOY_HOOK --body '<url>'`.

**Reality:** the deploy job was deleted (`ci: drop stale Render deploy-hook job — the service auto-deploys main on push`). [`.github/workflows/ci.yml`](../.github/workflows/ci.yml) has `lint → test → build → smoke`; no job calls a Render hook, and no `RENDER_DEPLOY_HOOK` secret is required or read anywhere in the repo. Deploys happen because Render's own GitHub integration watches `main` — pushing is the deploy.

**Why the hook went away:** it was a second, independent trigger for the same deploy. Two paths to production means a green CI run could report "deployed" while Render had already deployed the same commit itself, or (worse) fire a rebuild of a commit whose tests had not finished. Render's native auto-deploy is the single trigger.

**What replaced the verification the hook implied:** a `smoke` job (spec 12) that polls the deployed `/health` after the build. It is **env-gated**: with the `SMOKE_BASE_URL` repository variable unset it prints a skip line, makes zero HTTP requests, and exits `0`, so a fork PR or a sleeping / suspended free-tier service cannot fail CI for a reason unrelated to the diff.

**To close:** nothing — this is the intended shape. The deviation is recorded because five README sites and one doc site described the deleted pipeline as if it still existed.

---

## Deviations explicitly accepted for v0.1.0

These are intentional scope cuts that won't be closed in this repo:

- **No separate `slowquery-store` Neon branch.** Bookkeeping tables live on the same branch as the commerce data. The spec hinted at an "admin" branch but the complexity wasn't worth it for a portfolio demo.
- **No EXPLAIN ANALYZE — just EXPLAIN.** The drainer runs `EXPLAIN (FORMAT JSON)` without `ANALYZE` to avoid double-executing the query. `ANALYZE` would add real timings to the plan; without it we get estimated rows and costs which is enough for the rules engine.

### Withdrawn from this list (they shipped)

Two bullets that used to live above are no longer true and have been removed rather than left to rot:

- ~~"No live SSE endpoint for the dashboard."~~ `GET /_slowquery/api/stream` ships — [`api/routers/dashboard.py`](../src/slowquery_demo/api/routers/dashboard.py) `stream_fingerprints` / `_sse_generator`, spec 09.
- ~~"`/_slowquery/queries` returns an empty list right now (my local stub) because the library's `dashboard_router` is a lazy stub."~~ The demo owns a real read API: `GET /_slowquery/queries`, `GET /_slowquery/queries/{id}` and `POST /_slowquery/queries/{id}/force-explain` read the bookkeeping tables through [`repositories/slowquery_repository.py`](../src/slowquery_demo/repositories/slowquery_repository.py), and `install_slowquery` mounts *that* router at `/_slowquery` instead of the library's stub. Spec 08.

See [`docs/projects/50-slowquery-detective.md`](https://github.com/Abdul-Muizz1310/slowquery-detective/blob/main/docs/projects/50-slowquery-detective.md) for the authoritative spec.
