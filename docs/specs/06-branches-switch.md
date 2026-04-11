# Spec 06 — `POST /branches/switch`

## Goal

Expose a single endpoint that flips the demo service's active Neon branch between `slowquery` and `slowquery-fast` and rebuilds the SQLAlchemy engine + connection pool against the new branch. This is the endpoint the dashboard's "Apply index on fast branch" button calls. The live p95 line graph dropping from red to green within a few seconds is a function of this endpoint behaving correctly — it is the **only** piece of magic in the demo.

## Inputs / Outputs

- **Request:** `POST /branches/switch` with body `{"target": "slow" | "fast"}`.
- **Response (200):** `{"active": "slow" | "fast", "switched_at": "<ISO8601>", "latency_ms": <int>}` — `latency_ms` is the wall-clock time the switch took, useful for the dashboard to render a spinner that vanishes predictably.
- **Response (400):** target is not one of the two allowed values.
- **Response (409):** target is already the active branch (no-op refused, not silently acknowledged — we want the dashboard to know).
- **Response (503):** new pool fails health check within `SWITCH_TIMEOUT_MS` (default 10000).

## Implementation sketch

1. Acquire `app.state.switch_lock` (an `asyncio.Lock`) to serialize concurrent switch attempts.
2. If `target == app.state.branch_current`, return 409.
3. Resolve the new `DATABASE_URL` from settings (`slow → settings.database_url`, `fast → settings.database_url_fast`).
4. Build a new `AsyncEngine` against the new URL.
5. Run a `SELECT 1` against the new engine to confirm it's reachable; if this fails within `SWITCH_TIMEOUT_MS`, dispose of the new engine and return 503 (old engine stays in use).
6. Atomically swap `app.state.engine` → new engine, dispose of the old engine with a small grace period (5s) to let in-flight queries finish.
7. Update `app.state.branch_current = target`, persist to disk at `.branch_state` (see invariant 4).
8. Return 200.

The endpoint never touches the Neon API directly. The URL for each branch is wired at startup via env vars. This is deliberate — calling out to the Neon API from an HTTP handler is a latency hazard and a secrets-leak hazard. The Neon API is only used out-of-band by human operators when re-provisioning the branches.

## Invariants

1. Only targets `"slow"` and `"fast"` are accepted. Never accepts an arbitrary URL, never accepts a branch name, never accepts a compute endpoint id. The allowlist is enumerated in code as `Literal["slow", "fast"]`.
2. The switch is atomic: at any instant, either the old engine or the new engine is serving traffic, never both, never neither. Enforced by the `switch_lock`.
3. When the new engine fails its health check, the old engine continues serving. The response is 503 and the demo stays on the previous branch — we never strand the service.
4. The active branch survives process restarts. `app.state.branch_current` is mirrored to a one-line file (`.branch_state`, gitignored) that `create_app()` reads on boot. Default when the file is absent: `slow` (so fresh instances always start in "before" mode).
5. The slowquery-detective middleware's buffer is cleared on switch. Fingerprints and samples from the previous branch would be misleading alongside fresh stats from the new branch.
6. `query_fingerprints` etc. live on whichever branch `SLOWQUERY_STORE_URL` points at, which is independent of the switch. The switch only touches the demo engine, not the store engine.
7. The endpoint returns within `SWITCH_TIMEOUT_MS + 1000` under every path (success, failure, refused). No request can hang forever.
8. The endpoint is gated by `DEMO_MODE=true`. In non-demo mode it returns 403. This is enforced by the platform middleware's token check, not by ad-hoc code in the handler.

## Test cases

**Success (unit — in-memory fakes for engine construction):**
1. `POST /branches/switch {"target": "fast"}` when current is `slow` returns 200 and sets `app.state.branch_current = "fast"`.
2. A second `POST /branches/switch {"target": "fast"}` immediately after returns 409 (already active).
3. Concurrent switches (two requests in flight) serialize via the lock; the second one sees the first's result and returns 409.
4. The response body includes an ISO8601 `switched_at` and a positive integer `latency_ms`.
5. The buffer on `app.state.slowquery_buffer` is cleared after a successful switch.

**Success (integration — two Testcontainers Postgres instances acting as the two branches):**
6. Start app pointed at `slow`; the demo endpoints return rows from the slow container. `POST /branches/switch {"target": "fast"}` → subsequent demo endpoints return rows from the fast container.
7. Under matching seeds (Spec 03 invariant 5), the row identities (`(email, full_name, created_at)` etc.) returned before and after the switch are identical — proof the switch changes schema, not data.
8. `EXPLAIN` on a query through the API before and after the switch transitions from `Seq Scan` to `Index Scan`.
9. `slowquery-detective` starts recording new fingerprints against the new branch within one request after the switch.
10. After a process restart, the previously-switched branch is still active (`.branch_state` persisted).

**Failure / negative:**
11. `POST /branches/switch {"target": "banana"}` returns 422 (Pydantic rejection before the handler runs).
12. `POST /branches/switch {}` returns 422.
13. When the fast container is unreachable, switching to `fast` returns 503 within `SWITCH_TIMEOUT_MS + 1000`, the old engine is still serving, and `app.state.branch_current` is unchanged.
14. A malformed `.branch_state` file at boot (`"zonk"`) is discarded and the service starts on `slow` with a warning logged.

**Security / destructive-guard:**
15. The endpoint body is a Pydantic model with `Literal["slow", "fast"]`. No raw string processing. A `Literal` mismatch is a 422, not a 400.
16. `DEMO_MODE=false` returns 403 even if the body is well-formed. Enforced by platform middleware; test covers both paths.
17. The handler never reads `DATABASE_URL` or `DATABASE_URL_FAST` from the environment at request time — they're captured once at startup in `Settings`. A test patches `os.environ` mid-test and confirms the switch endpoint uses the startup values.
18. The endpoint does **not** accept DDL. There is no `--apply-index` payload, no `sql` field. The "apply index" button on the dashboard is a branch switch, not a DDL execution.
19. An attacker sending `POST /branches/switch` in a tight loop cannot exhaust the new-engine construction path because the lock serializes. The handler is still O(1) per request.

## Acceptance

- [ ] `src/slowquery_demo/api/routers/branches.py` holds the route.
- [ ] `src/slowquery_demo/services/branch_switcher.py` holds the engine swap logic (pure async, no HTTP concepts).
- [ ] `src/slowquery_demo/schemas/branches.py` holds the `SwitchBranchRequest` and `SwitchBranchResponse` Pydantic models.
- [ ] `src/slowquery_demo/core/branch_state.py` handles `.branch_state` persistence.
- [ ] `SWITCH_TIMEOUT_MS` and `DEMO_MODE` are both `Settings` fields.
- [ ] Every test case above has a corresponding test.
- [ ] The dashboard's "apply on fast branch" flow is end-to-end-tested against two Testcontainers Postgres instances.
