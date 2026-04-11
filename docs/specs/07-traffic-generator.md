# Spec 07 — Traffic generator

## Goal

Produce a continuous stream of realistic HTTP traffic against the deployed demo service so that when someone opens the dashboard cold, there are already fingerprints, rolling p95 stats, explain plans, and suggestions to look at. The generator also has to produce the specific query shapes the rules engine is designed to catch: seq scans on `orders.user_id`, seq scans on `order_items.{order_id,product_id}`, an `ORDER BY created_at` without a composite index, and an N+1 pattern (many `/users/{id}/orders` calls in a short burst). Without this the demo is a museum piece — the first person to load it sees an empty dashboard.

## Inputs / Outputs

- **Deliverable:** `scripts/traffic_generator.py` — a Locust file (`from locust import HttpUser, task, between`) plus a thin `__main__` that runs it headless against a configurable `--host`.
- **In:** `TARGET_URL` (env, defaults to `http://localhost:8000`), `TRAFFIC_DURATION_SECONDS` (default 60), `TRAFFIC_USERS` (default 20), `TRAFFIC_SPAWN_RATE` (default 5 users/s).
- **Out:** stdout metrics from Locust (JSON when `--json` flag is passed), exit `0` on success.
- **Deployment:** Render cron worker configured in `render.yaml` (`type: cron`, schedule `0 * * * *` — hourly 60-second burst). The cron job runs `uv run python scripts/traffic_generator.py --host $TARGET_URL --json`.

## Traffic shape (the point of the whole script)

| Pattern | Endpoint | Weight | Rule it should fire on slow branch |
|---|---|---|---|
| User profile lookup | `GET /users/{id}` | 10 | — (fast path) |
| **Seq scan on `orders.user_id`** | `GET /users/{id}/orders` | 25 | `seq_scan_large_table` |
| **ORDER BY without index** | `GET /orders` (returns recent orders) | 15 | `sort_without_index` |
| **Seq scan on `order_items.order_id`** | `GET /orders/{id}` (joins to items) | 20 | `seq_scan_large_table` |
| **Seq scan on `order_items.product_id`** | `GET /order_items?product_id=...` | 15 | `seq_scan_large_table` |
| **N+1 burst** | 50× `GET /users/{id}/orders` in 1 second | 5 | `n_plus_one` |
| Product lookup | `GET /products/{id}` | 10 | — (fast path) |

Weights sum to 100 for readability. `HttpUser` tasks use the `@task(weight)` decorator with these weights.

Every random id used by the tasks is drawn from two small arrays prefetched at `on_start`: `self.known_user_ids`, `self.known_product_ids` (20 of each — the generator does a one-time `GET /users?limit=20` and `GET /products?limit=20` on user start, storing the returned ids). This avoids 404-storming the service with UUIDs that don't exist.

## Invariants

1. The generator **never** hits `/health`, `/_slowquery/*`, or `/branches/switch`. The point is to produce observability load against the commerce endpoints only.
2. The generator runs headless (`--headless`) and terminates cleanly after `TRAFFIC_DURATION_SECONDS`. It does not loop forever — the cron worker handles repetition.
3. Each `HttpUser` task handles 4xx responses gracefully (logs at `info`, does not retry, does not crash the task). The service's pagination cap (Spec 04 test 13) is not a bug, it's a check.
4. The generator's 5xx rate is a health signal: the Locust run's exit code becomes non-zero if p95 > 30000ms OR failure rate > 20%, so a catastrophically broken demo surfaces as a red cron run in Render.
5. The N+1 burst task uses `asyncio.gather` or Locust's `task` decorator with an explicit for-loop; it must result in ≥50 fingerprint samples for the same parameterized query within 1 second (so the library's N+1 rule fires).
6. The generator itself does not assume anything about the active branch. It issues the same traffic in both modes. The difference between `slow` and `fast` is **only** visible in the slowquery dashboard, not in the generator's output.
7. The `--host` flag is respected in both local-dev and Render-cron-worker modes. Render config injects `TARGET_URL`; the script falls back to `http://localhost:8000` when running locally.

## Test cases

**Success (unit — pure helpers, no HTTP):**
1. `choose_weighted(rng, tasks)` returns each task with probability proportional to its weight across 10k samples (binomial test within expected range).
2. `parse_args([])` returns defaults; `parse_args(["--users", "50", "--json"])` overrides them.
3. The list of enabled tasks does not include `/health` or `/_slowquery/*` (grep self-test on the task method names).

**Success (integration — real service, mocked upstream via `TestClient` or a live local uvicorn):**
4. Running `traffic_generator --host http://localhost:<port> --users 2 --duration 10` against a running demo service produces ≥1 request of each of the 7 task types within the 10-second window.
5. After the run, `GET /_slowquery/queries` returns ≥5 distinct fingerprints.
6. The N+1 burst task produces ≥50 samples for the same fingerprint within a single second (verified by inspecting `query_samples.sampled_at`).
7. With `DEMO_MODE=false`, the generator still runs — it doesn't depend on demo mode.

**Failure / negative:**
8. Running with an invalid `--host` (unreachable) exits `1` with a clear error and does not silently log `0 requests succeeded`.
9. A run where p95 > 30000ms exits `1` (health signal invariant 4).
10. A run where failure rate > 20% exits `1` (health signal invariant 4).
11. If `/users?limit=20` returns an empty list (fresh unseeded DB), the generator exits `1` with "no seed data, run scripts/seed_slow.py first".

**Security / destructive-guard:**
12. The generator does not `POST /branches/switch`. A grep self-test asserts `/branches/switch` does not appear in the script.
13. The generator does not send an `X-Platform-Token` header — it relies on `DEMO_MODE=true` accepting any token. In a hypothetical non-demo deployment, the generator would 403 loud, which is a feature: no one runs this against production.
14. Locust's stats upload endpoints are disabled (`--no-web` is implied by `--headless`).

## Acceptance

- [ ] `scripts/traffic_generator.py` is a single-file Locust driver + `__main__`.
- [ ] Renders `render.yaml` includes a `type: cron` worker entry pointing at this script with the hourly schedule.
- [ ] Every task weight above is a named constant (`WEIGHT_USER_PROFILE = 10`, etc.) — not magic numbers inline.
- [ ] Every test case above has a corresponding test.
- [ ] The generator's first run after a successful deploy produces ≥3 suggestions in the `suggestions` table — verified by the S6 acceptance script.
