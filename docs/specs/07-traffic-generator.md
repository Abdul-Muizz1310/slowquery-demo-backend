# Spec 07 — Traffic generator and scheduled traffic

> **Rewritten 2026-10-20 to describe what ships.** The first version of this spec promised a Locust file run
> by a Render cron worker. Neither was built: the driver is a single-file **httpx** script, and Render's
> free plan has no cron service in this blueprint. The history of that gap is
> [DEVIATIONS §10](../DEVIATIONS.md). Test numbers 1–14 keep their old meaning where the behaviour is the
> same, so the docstrings in `tests/**/test_07_traffic_generator.py` still point at the right case.

## Goal

Produce realistic commerce traffic against the demo service so that someone opening the dashboard cold
finds fingerprints, rolling p95 stats, EXPLAIN plans and suggestions, not an empty table. The traffic has to
include the query shapes the rules engine is built to catch: scans on `orders.user_id` and
`order_items.{order_id,product_id}`, an `ORDER BY created_at` with no index, and an N+1-style burst of
`/users/{id}/orders` calls.

Two pieces ship:

1. `scripts/traffic_generator.py` — the driver, run by hand or by the schedule.
2. `.github/workflows/traffic.yml` — a GitHub Actions schedule that wakes the live service and runs the
   driver against it every six hours, so the live dashboard never shows data older than about six hours.

## The driver

- **CLI:** `python -m scripts.traffic_generator [--host URL] [--duration S] [--users N] [--json]`.
  Defaults: `--host http://localhost:8000`, `--duration 60`, `--users 20` (sets pacing: one task every
  `1/users` seconds), `--json` off. No environment variables are read.
- **Client:** one `httpx.AsyncClient(base_url=host, timeout=5.0)`. Before the loop it prefetches
  `GET /users?limit=20` and `GET /products?limit=20` and keeps the returned ids, so tasks hit rows that
  exist. With an empty seed it falls back to random UUIDs (the requests then 404, which still exercises the
  queries).
- **Task mix** (weights are named constants in the script):

| Task | Requests | Weight | Query shape on the slow branch |
|---|---|---|---|
| `user_profile` | `GET /users/{id}` | 10 | primary-key lookup (fast path) |
| `user_orders` | `GET /users/{id}/orders` | 25 | scan on `orders.user_id` |
| `recent_orders` | `GET /orders?limit=20` | 15 | `ORDER BY created_at` without an index (`sort_without_index`) |
| `order_with_items` | `GET /orders?limit=5`, then `GET /orders/{id}` | 20 | scan on `order_items.order_id` |
| `items_by_product` | `GET /order_items?product_id=…` | 15 | scan on `order_items.product_id` |
| `n_plus_one_burst` | 50 × `GET /users/{id}/orders`, sequential | 5 | many calls of one fingerprint |
| `product_profile` | `GET /products/{id}` | 10 | primary-key lookup (fast path) |

- **Output:** with `--json`, one line `{"total", "failures", "p95_ms", "exit_code"}`. `p95_ms` is wall-clock
  per task from the caller's side. A transport error (timeout, refused connection) counts as a failure; an
  HTTP 4xx/5xx does not.
- **Exit code:** `1` when no task ran, when the failure rate exceeds 20 %, or when p95 exceeds 30 000 ms;
  otherwise `0`. A broken demo therefore shows up as a red run wherever the driver is scheduled.

## The schedule

`.github/workflows/traffic.yml`:

- **Triggers:** `schedule` with cron `0 */6 * * *` (every six hours, UTC) and `workflow_dispatch`. Nothing
  else: it never runs on `push` or `pull_request`, so a fork or a feature branch cannot spend the live
  service's free-tier hours.
- **Target:** exactly `https://slowquery-demo-backend.onrender.com`, a constant in the workflow, not a secret
  or variable.
- **Wake first:** Render's free instance sleeps after 15 idle minutes and takes up to about 90 s to boot. A
  wake step runs `scripts/smoke_health.py` (spec 12) with `SMOKE_BASE_URL` set to the target, polling
  `/health` with a budget of at least 90 s (`SMOKE_ATTEMPTS × SMOKE_DELAY_S`). If the service never becomes
  healthy the job fails there, before generating traffic.
- **Then drive:** `python -m scripts.traffic_generator --host <target> --duration 60 --users 3 --json`. The
  driver's exit code is the step's exit code.
- **Hygiene:** `permissions: contents: read`, a job `timeout-minutes`, and a `concurrency` group so a manual
  run and a scheduled one never overlap. The workflow references no secrets: the routes it calls are the
  public read-only commerce routes (platform-contract-v2 §2), so it needs no token.

## Invariants

1. The driver only issues `GET` requests to the commerce routes (`/users…`, `/products…`, `/orders…`,
   `/order_items…`). It never calls `/health`, `/version`, `/metrics`, `/_slowquery/*` or `/branches/*`, and
   never sends a write.
2. The driver stops at its `--duration` deadline; it does not loop forever. Repetition is the schedule's job.
3. The driver never sends an `X-Platform-Token` (it has none to send).
4. The driver's exit code is the health signal of invariant "exit code" above.
5. The driver is branch-agnostic: the same traffic in `slow` and `fast` mode; the difference shows only in
   the dashboard.
6. The schedule wakes the service before driving it, and targets only the live URL.

## Test cases

**Unit (driver — no network; `respx` intercepts httpx):**
1. `choose_weighted` returns each task in proportion to its weight over 10k samples.
2. `parse_args([])` returns the defaults; `parse_args(["--users", "50", "--json"])` overrides them.
3. Running `_run_driver` issues requests only to commerce paths (invariant 1).
8. An unreachable `--host` exits `1` (`@slow`: spawns a subprocess).
9. Stats with p95 > 30 000 ms → exit `1`.
10. Stats with failure rate > 20 % → exit `1`.
11. Empty seed data: the driver still runs, using fallback ids.
12. No request is a write and none touches `/branches/*` (invariant 1).
13. No request carries `X-Platform-Token` (invariant 3).
14. The driver terminates at its deadline (invariant 2).

**Integration (Testcontainers Postgres + a live local uvicorn):**
4. A 10-second run produces at least one request of each task type.
5. After a run, `GET /_slowquery/queries` lists at least five fingerprints.
6. The N+1 burst shows as one hot fingerprint: the user-orders statement reaches a call count of at least
   50 in a 15-second run (slowquery-detective 0.2 keeps per-fingerprint aggregates, spec 15).
7. The driver runs unchanged against a service with `DEMO_MODE=false`.

**Unit (schedule — the workflow file, parsed as YAML):**
15. Triggers are exactly `schedule` (one cron, `0 */6 * * *`) and `workflow_dispatch`.
16. The wake step runs `scripts.smoke_health` with `SMOKE_BASE_URL` equal to the target and a polling budget
    of at least 90 s, and comes before the traffic step.
17. The traffic step runs `scripts.traffic_generator` with `--host` equal to the target, a positive
    `--duration`, and `--json`.
18. The target is `https://slowquery-demo-backend.onrender.com`; the workflow mentions no `secrets.`, has
    `permissions: contents: read`, a job timeout and a concurrency group.
