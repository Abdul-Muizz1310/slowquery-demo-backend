# Spec 11 — Gating the state-mutating endpoints on a public URL

> **Written after the fact.** Specs 00–10 were written before their code. This one was not: the
> mutation gates in [`core/access.py`](../../src/slowquery_demo/core/access.py) shipped in response to
> an audit, driven by [`tests/unit/test_11_mutation_gating.py`](../../tests/unit/test_11_mutation_gating.py),
> and that test file referenced a "Spec 11" that did not exist. This document is that spec, reconstructed
> from the shipped behaviour and its tests so the numbering in the test file resolves to something real.
> It is the one spec in `docs/specs/` that did **not** precede its implementation.

## Goal

`DEMO_MODE=true` makes the platform-token middleware accept every request, which is what lets a stranger
click through the demo. That also left the two endpoints with lasting, cross-visitor side effects
unauthenticated on a public URL:

- `POST /_slowquery/queries/{id}/force-explain` writes a **synthetic** EXPLAIN plan
  (`{"Plan": {"Node Type": "Result", "Total Cost": 0.01}}`) and can overwrite a genuine captured plan —
  permanent corruption of the thing the demo exists to show.
- `POST /branches/switch` rebuilds the `AsyncEngine` against the other Neon branch. Spammed, it is
  unbounded engine-rebuild churn against free-tier Neon and Render.

Gate both **without** taking the demo away: the branch switch is the punchline and has to stay clickable.

## Inputs / Outputs

- **In (env):** `DEMO_MUTATION_TOKEN` (default unset), `DEMO_MUTATION_COOLDOWN_S` (float seconds,
  `ge=0`, default `0.0` — `0` disables the cooldown so unit tests stay deterministic).
- **In (request):** optional `X-Admin-Token` header.
- **Out:** the route's normal response, or `403` (gate refused) / `429` (inside the cooldown window).

## Shape

Two composable FastAPI dependencies in `core/access.py`, both reading typed `Settings` off `app.state` —
never `os.environ` at request time:

| Gate | Applied to | Behaviour with **no** token configured | Behaviour with a token configured |
|---|---|---|---|
| `enforce_cooldown` | both | passes when `DEMO_MUTATION_COOLDOWN_S == 0`, else one call per client per window | unchanged (independent of the token) |
| `require_admin_token` | `force-explain` (destructive) | **403 — fail closed**, endpoint disabled | requires a matching `X-Admin-Token` |
| `require_admin_token_if_configured` | `branches/switch` (transient) | passes — stays public | requires a matching `X-Admin-Token` |

`CooldownLimiter` is a per-key `time.monotonic()` window held on `app.state.mutation_limiter`; the key is
the client IP (`"unknown"` when Starlette reports no client).

## Invariants

1. Both gates apply **even under `DEMO_MODE`**. They are route dependencies, not special cases inside the
   token middleware — the middleware stays single-purpose.
2. `force-explain` is fail-closed: unconfigured means disabled (403), never "allow". An anonymous visitor
   can never overwrite a captured plan.
3. `branches/switch` is fail-open **only** because its damage is transient and bounded by the cooldown.
   Setting `DEMO_MUTATION_TOKEN` locks it down too, with no code change.
4. Token comparison is constant-time (`hmac.compare_digest`). A missing / empty header never compares equal.
5. `CooldownLimiter(cooldown_s)` rejects a negative window with `ValueError` at construction — an illegal
   configuration fails at startup, not at the first request.
6. Cooldown keys are independent: throttling one client never throttles another.
7. A `cooldown_s` of `0` disables the limiter entirely (every call allowed).
8. `enforce_cooldown` is a no-op when no limiter is wired onto `app.state` — a partially-wired app degrades
   to "no throttle", never to `AttributeError`.
9. Read-only routes are never gated. `GET /branches/current` (spec 06) carries neither dependency, so a
   dashboard can poll the active branch without consuming the mutation cooldown.

## Test cases

**Success / refusal (composed request path — real `create_app`, mocked DB session):**
1. With no `DEMO_MUTATION_TOKEN`, `POST /_slowquery/queries/{id}/force-explain` returns 403.
2. With a token configured, a **wrong** `X-Admin-Token` returns 403 before any store write.
3. With a token configured, the matching `X-Admin-Token` clears the gate (the response is neither 403 nor
   429; the handler then proceeds to the store).
4. With `DEMO_MUTATION_COOLDOWN_S=60`, the first `POST /branches/switch` returns 200 and a second one
   inside the window returns 429.
5. With no token and no cooldown, `POST /branches/switch` stays public (200) — the demo punchline works.
6. Configuring `DEMO_MUTATION_TOKEN` locks branch switch: 403 without the header, 200 with it.

**Pure-helper units:**
7. `CooldownLimiter(0.0).allow(k)` is always `True`.
8. `CooldownLimiter(60.0)`: first `allow("a")` is `True`, the second is `False`, and `allow("b")` is `True`.
9. `CooldownLimiter(-1.0)` raises `ValueError`.
10. `require_admin_token` raises `HTTPException(403)` when unconfigured.
11. `require_admin_token` passes when the provided token matches.
12. `require_admin_token_if_configured` passes when no token is configured.
13. `enforce_cooldown` raises `HTTPException(429)` on the second call inside the window.
14. `enforce_cooldown` is a no-op when `app.state` carries no limiter.

## Acceptance

- [x] `src/slowquery_demo/core/access.py` holds `CooldownLimiter`, `enforce_cooldown`,
      `require_admin_token`, `require_admin_token_if_configured`.
- [x] `main.create_app` wires `app.state.mutation_limiter = CooldownLimiter(settings.demo_mutation_cooldown_s)`.
- [x] `force-explain` declares `Depends(enforce_cooldown)` + `Depends(require_admin_token)`;
      `branches/switch` declares `Depends(enforce_cooldown)` + `Depends(require_admin_token_if_configured)`.
- [x] `DEMO_MUTATION_TOKEN` and `DEMO_MUTATION_COOLDOWN_S` are typed `Settings` fields.
- [x] `render.yaml` ships `DEMO_MUTATION_COOLDOWN_S=3` and leaves `DEMO_MUTATION_TOKEN` unset
      (`sync: false`) so `force-explain` is disabled until an operator provisions it.
- [x] Every test case above has a corresponding test in `tests/unit/test_11_mutation_gating.py`.
