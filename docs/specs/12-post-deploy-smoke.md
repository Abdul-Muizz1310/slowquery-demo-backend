# Spec 12 — Post-deploy `/health` smoke check

## Goal

The Render service auto-deploys `main` on push (there is no deploy job in CI — see
[DEVIATIONS §11](../DEVIATIONS.md)). Nothing currently verifies that the container that came up actually
serves traffic, so a boot-time `ConfigError` or an unreachable Neon branch would land on `main` green.

Add a CI step that polls the deployed `/health` after the build and fails the workflow when the service is
not ready. It must be **inert by default**: the URL comes from an environment variable, and when that
variable is unset the check reports "skipped" and exits `0`. A fork PR, a local run, or a period where the
Render service is intentionally down must not turn CI red for a reason unrelated to the diff.

## Inputs / Outputs

- **In (env):** `SMOKE_BASE_URL` — the deployed service's base URL (e.g.
  `https://slowquery-demo-backend.onrender.com`). Unset or blank ⇒ skip.
- **In (env, optional):** `SMOKE_ATTEMPTS` (default 10), `SMOKE_DELAY_S` (default 15.0). Render Free cold
  boots take ~30 s, so the default budget is ~2.5 minutes of polling.
- **Out (stdout):** one line per attempt plus a final verdict. No secrets — only the base URL, which is public.
- **Out (exit code):** `0` when skipped or healthy, `1` when the budget is exhausted without a healthy
  response.

## Shape

```python
# scripts/smoke_health.py — imperative shell around two pure predicates.

SMOKE_URL_ENV = "SMOKE_BASE_URL"

def health_verdict(status_code: int, payload: object) -> str: ...   # "healthy" | "degraded" | "malformed"
def health_url(base_url: str) -> str: ...                           # base + "/health", one slash
def main(argv: list[str] | None = None) -> int: ...                 # polls, prints, returns exit code
```

`/health` already returns `200` + `{"status": "ok", "db": "ok", ...}` when ready and `503` +
`{"status": "degraded", "db": "down", ...}` when the DB probe fails
([`core/platform.py`](../../src/slowquery_demo/core/platform.py)), so the verdict is derived from the
status code **and** the `db` field — a 200 whose body says `db: down` is not healthy.

## Invariants

1. **Unset ⇒ skip, and no HTTP request is made at all.** Not "request and ignore failures" — zero outbound
   calls, so the step cannot fail or hang when the URL is absent. A blank / whitespace-only value counts as unset.
2. The check is read-only: it issues `GET /health` and nothing else. It never mutates the demo.
3. A non-JSON or non-object body is `"malformed"`, never `"healthy"` — the check fails closed on garbage.
4. `health_url` joins exactly one `/` regardless of a trailing slash on the base URL.
5. Transport errors (DNS, refused, timeout) are retried like a `503`, not raised — a cold-booting Render
   container looks the same as an unreachable one until the budget runs out.
6. The exit code is the only contract CI depends on. Log lines are for humans.
7. No secret is ever printed. `SMOKE_BASE_URL` is a public URL; nothing else is read from the environment.

## Test cases

**Pure predicates:**
1. `health_verdict(200, {"status": "ok", "db": "ok"})` is `"healthy"`.
2. `health_verdict(503, {"status": "degraded", "db": "down"})` is `"degraded"`.
3. `health_verdict(200, {"status": "ok", "db": "down"})` is `"degraded"` — the status code alone is not enough.
4. `health_verdict(200, ["not", "an", "object"])` and `health_verdict(200, None)` are `"malformed"`.
5. `health_verdict(500, {})` is `"degraded"`.
6. `health_url("https://x.example")` and `health_url("https://x.example/")` both give
   `"https://x.example/health"`.

**Skip path (invariant 1):**
7. With `SMOKE_BASE_URL` unset, `main([])` returns `0` **and** `respx` records zero calls.
8. With `SMOKE_BASE_URL="   "`, same as unset.

**Poll path:**
9. A healthy first response ⇒ `main` returns `0` after exactly one request.
10. Two `503`s then a `200` ⇒ `main` returns `0` after three requests (`SMOKE_ATTEMPTS=3`, `SMOKE_DELAY_S=0`).
11. `SMOKE_ATTEMPTS=2` with every response `503` ⇒ `main` returns `1` after exactly two requests.
12. A transport error on every attempt ⇒ `main` returns `1` (invariant 5), no exception escapes.

## Acceptance

- [x] `scripts/smoke_health.py` holds the two pure predicates plus `main`.
- [x] `.github/workflows/ci.yml` runs it in a `smoke` job that `needs: build`, passing
      `SMOKE_BASE_URL: ${{ vars.SMOKE_BASE_URL }}` so an unset repository variable skips the check.
- [x] Every test case above has a corresponding test in `tests/unit/test_12_post_deploy_smoke.py`.
