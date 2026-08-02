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

## 3. `POST /branches/switch` rebuilds the commerce engine, but the observability EXPLAIN pool does not follow the swap

**Spec says:** "Branch switch toggles between slow and fast demos … uses Neon API to swap between the two branches".

**Repo ships (current):** the endpoint validates `target ∈ {slow, fast}` with a `Literal` type, serialises concurrent switches via `asyncio.Lock`, persists `app.state.branch_current` to a `.branch_state` file so restarts respect the choice, and returns a `SwitchBranchResponse` with `active`, `switched_at`, and `latency_ms`. It **does** rebuild the SQLAlchemy `AsyncEngine` / `async_sessionmaker` against the target branch's URL: `main._make_engine_builder` builds a new engine, health-checks it with `SELECT 1` (5s timeout), atomically swaps `app.state.engine` + `app.state.db_sessionmaker`, and disposes the old engine after a 5s grace window. `core/database.get_db` reads `app.state.db_sessionmaker` per request, so commerce queries (`/users`, `/orders`, `/users/{id}/orders`) follow the switch. After a successful swap the switcher's `post_switch` hook (`main._make_post_switch` → `observability.on_branch_switch`) also **clears `app.state.slowquery_buffer`** (spec 06 invariant 5) and **re-attaches the slowquery listeners to the new engine** (`observability.reattach_slowquery`), so the rolling percentiles reset and new in-process samples start recording against the new branch within one request.

This closes the *old* version of this deviation — the engine rebuild, buffer clear, and listener re-attach all ship now and are unit-tested (`tests/unit/test_06_branches_switch.py`, `tests/unit/test_10_engine_rebuild.py`, `tests/unit/test_05_slowquery_install.py`).

**Remaining gap:** the drainer's direct-EXPLAIN path is **not** repointed at the new branch. `core/observability._run_direct_explain` runs `EXPLAIN (FORMAT JSON)` through `store._ensure_pool()` — the same asyncpg pool the `PostgresStoreWriter` uses for the bookkeeping tables, keyed on `SLOWQUERY_STORE_URL` (default `DATABASE_URL`). Per spec 06 invariant 6 the bookkeeping store is *deliberately* independent of the switch, but EXPLAIN piggybacks on that pool, so after switching to `fast` the captured plans are still produced against the slow branch's schema. The persisted percentiles (from the in-process listeners, which do follow the swap) are correct for the new branch; the persisted EXPLAIN plan / cost / rule-suggestions for a fingerprint can lag one branch behind until the store pool is separated from the EXPLAIN pool.

**Why the EXPLAIN repoint is deferred:** splitting the EXPLAIN pool from the bookkeeping store pool means giving the drainer its own pool keyed on the *active commerce* URL and rebuilding/repointing it on every switch, inside the drainer's critical section. That change cannot be verified without two live branches (Testcontainers / Neon), and shipping it untested risks a half-broken EXPLAIN path that fails silently. It is left as a contained follow-up rather than guessed at.

**To close:** give the drainer a dedicated asyncpg pool built from `app.state.engine`'s active URL (not the store URL), repoint it inside `on_branch_switch`, and verify with the integration lane (spec 06 test 8/9 already EXPLAIN before and after a switch against two Testcontainers Postgres instances).

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

## 6. `render.yaml` `preDeployCommand` is silently ignored on Free tier

**Spec says (implicitly):** Render runs `alembic upgrade head` as a pre-deploy step.
**Reality:** `preDeployCommand` is gated behind Render paid tiers. On Free the field is accepted by the YAML parser but never executed. First migration ran manually from a dev machine.

**Full write-up with Option A/B/C trade-off table:** [`docs/RENDER_FREE_TIER_MIGRATIONS.md`](RENDER_FREE_TIER_MIGRATIONS.md).

**To close:** move alembic into the Dockerfile `CMD` when schema churn picks up (see the doc for the exact shape).

## 7. Integration lane is committed but Docker-gated

**Spec implies:** full test matrix runs in CI.
**Repo ships:** 221 unit tests run in CI on every push; 49 integration tests live under `tests/integration/` and are filtered out of the default run. They're committed and would work locally given a running Docker Desktop — the session-scoped `pg_container` fixture boots a Testcontainers Postgres, `alembic upgrade head` runs against it via subprocess to avoid event-loop collision with pytest-asyncio.

**Why:** Docker Desktop wasn't running on my dev laptop during S5a when the conftest landed. Adding a CI job that boots Docker-in-Docker is a separate line item.

**To close:** add a second CI job that runs `uv run pytest -m integration` with a `services.postgres` entry or a Docker-in-Docker setup. The fixture is ready for it.

## 8. Four live production bugs fixed by in-repo shims rather than upstream PRs

**Reality:** [`core/observability.py`](../src/slowquery_demo/core/observability.py) contains four module-level monkey-patches that rewrite slowquery-detective v0.1.0's behaviour. Each is documented inline with a rationale. The right long-term fix is to upstream these to the library itself:

1. Replace `app.add_event_handler` with FastAPI lifespan.
2. Accept an optional `store: StoreWriter | None` parameter on `install()`.
3. Rewrite `hooks.attach` to stash state via `setattr(context)` rather than `cursor.info`.
4. Rewrite `ExplainWorker._run_explain` to accept real captured parameters or integrate the bridge queue pattern from shim 4.

**To close:** open PRs against the `slowquery-detective` repo. Until then, the shims stay in this repo's `core/observability.py` and are versioned alongside the library they patch.

## 9. State-mutating endpoints are hardened for the public URL

**Context:** `DEMO_MODE=true` bypasses the platform-token middleware for every route, so on the live URL the two endpoints with lasting cross-visitor side effects were unauthenticated. `POST /_slowquery/queries/{id}/force-explain` could overwrite a genuine captured EXPLAIN plan with a synthetic stub (permanent corruption), and `POST /branches/switch` could be spammed into repeated engine-rebuild churn against free-tier Neon/Render.

**Repo ships (now):** both are gated in [`core/access.py`](../src/slowquery_demo/core/access.py), enforced *even under `DEMO_MODE`*:

- A per-client cooldown (`DEMO_MUTATION_COOLDOWN_S`, seconds; `0` disables) throttles both endpoints and returns `429` inside the window.
- `force-explain` (destructive) is **fail-closed**: with no `DEMO_MUTATION_TOKEN` configured it returns `403`, so an anonymous visitor can never clobber real data. With a token set it requires a matching `X-Admin-Token` header (constant-time compare).
- `branches/switch` (transient churn, bounded by the cooldown) stays public by default so the demo punchline works, but is locked to the same admin token the moment `DEMO_MUTATION_TOKEN` is set.

`render.yaml` ships `DEMO_MUTATION_COOLDOWN_S=3`; `DEMO_MUTATION_TOKEN` is left unset (`sync: false`) so `force-explain` is disabled until an operator provisions it in the Render dashboard.

**Note vs. the audit's wording:** the fix is implemented as route-level dependencies rather than special-casing paths inside the token middleware — it achieves the same "require a secret for mutation even in demo mode" intent while keeping the middleware single-purpose.

## 10. Traffic generator is httpx-based, not a Locust file

**Spec says** ([`docs/specs/07-traffic-generator.md`](specs/07-traffic-generator.md)): the deliverable is "a Locust file (`from locust import HttpUser, task, between`) plus a thin `__main__` that runs it headless against a configurable `--host`" (spec 07 line 9), the generator "runs headless (`--headless`)" (invariant 2), stdout is "metrics from Locust" (line 11), and "Locust's stats upload endpoints are disabled (`--no-web` is implied by `--headless`)" (test 14).

**Repo ships:** [`scripts/traffic_generator.py`](../scripts/traffic_generator.py) is a single-file **httpx** driver. `locust` is not a dependency (`grep -rn locust src/ scripts/` finds no import) and there is no `HttpUser` / `@task` class. The CLI is `--host`, `--duration`, `--users`, `--json` — there is no `--headless`, no `--no-web`, no `--spawn-rate`, and the env vars the spec names (`TARGET_URL`, `TRAFFIC_DURATION_SECONDS`, `TRAFFIC_USERS`, `TRAFFIC_SPAWN_RATE`) are not read; `--host` defaults to `http://localhost:8000` in code. Output under `--json` is one line from this repo, not Locust: `{"total", "failures", "p95_ms", "exit_code"}`.

**Why:** the whole generator is ~200 lines of `asyncio` + `httpx`, and `httpx` is already a runtime dependency (the app ships it). Adding Locust would add a heavyweight test-only dependency, a gevent-based worker model that fights `asyncio`, and a web UI whose only role would be to be switched off. Spec invariant 4's health-signal contract (exit non-zero when p95 > 30 000 ms or failure rate > 20 %) is what actually matters for the Render cron worker, and `exit_code_for_stats` implements it directly.

**What this cost:** spec 07's Locust-specific invariants have no implementation to point at. Test 14 (`--no-web` implied by `--headless`) was for a while satisfied by a dead `_GREP_MARKER_HEADLESS = "--headless"` constant planted in the script purely so a source-grep assertion would pass — a test asserting a literal instead of behaviour. Both the constant and the grep assertions are gone: spec 07 tests 3 / 12 / 13 / 14 now drive `_run_driver` under `respx` and assert on the requests the loop actually issues (commerce paths only, `GET` only, no `X-Platform-Token`, terminates at the `--duration` deadline). "Headless" is not a mode here — it is the only mode, since there is no web UI to disable.

**To close:** either rewrite the script as a real Locust file and keep the spec, or rewrite spec 07 around the httpx driver (weights, `--json` shape, exit-code contract) and drop the Locust language. The second is the honest option; it is not done yet because the spec doubles as the record of what was originally promised.

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
