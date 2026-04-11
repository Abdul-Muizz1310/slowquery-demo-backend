# Spec 05 — Installing `slowquery-detective`

## Goal

Wire `slowquery-detective` v0.1.0 into the demo service as a 3-line integration (plus a fourth line for the dashboard router mount) — matching the library's documented public API. Configuration comes from environment variables, the store is the `PostgresStoreWriter` from Spec 01, and the LLM fallback is OpenRouter-backed (the library's `LlmConfig`). This is the smallest amount of code that turns the demo service into a live slowquery observability surface.

## Inputs / Outputs

- **In (env):** `DATABASE_URL`, `SLOWQUERY_THRESHOLD_MS` (default 100), `SLOWQUERY_SAMPLE_RATE` (default 1.0), `SLOWQUERY_STORE_URL` (defaults to `DATABASE_URL`), `LLM_FALLBACK_ENABLED` (default false), `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL_PRIMARY`.
- **Out:** a `FastAPI` app with
  - a slowquery-detective middleware active on the shared `AsyncEngine`,
  - `dashboard_router` mounted at `/_slowquery`,
  - an `install_slowquery(app)` call that runs exactly once from `create_app()` and is idempotent.

## Shape

```python
# src/slowquery_demo/core/observability.py

from slowquery_detective import install, dashboard_router
from slowquery_detective.llm_explainer import LlmConfig

def install_slowquery(app: FastAPI, engine: AsyncEngine, settings: Settings) -> None:
    llm_config = _build_llm_config(settings) if settings.llm_fallback_enabled else None
    store = PostgresStoreWriter(store_url=settings.slowquery_store_url or settings.database_url)
    install(
        app,
        engine,
        threshold_ms=settings.slowquery_threshold_ms,
        sample_rate=settings.slowquery_sample_rate,
        store=store,
        enable_llm=settings.llm_fallback_enabled,
        llm_config=llm_config,
    )
    app.include_router(dashboard_router, prefix="/_slowquery")
```

The `install` call is the library's public API — not patched, not wrapped. Any drift in the library's signature surfaces immediately in mypy.

## Invariants

1. `install_slowquery` is called exactly once per app lifetime, from `create_app()`, after the engine is built but before any test hits the app. A second call is a no-op (the library's `install()` is idempotent via `app.state._slowquery_installed`).
2. `SLOWQUERY_STORE_URL` defaults to `DATABASE_URL` when unset. This is the common path: bookkeeping tables live on the same branch as the demo data (they're not the bottleneck).
3. `LLM_FALLBACK_ENABLED=false` means the middleware runs rules-only and never issues an HTTP request to OpenRouter. Verified by running the integration tests with `LLM_FALLBACK_ENABLED=false` and confirming `respx` (as a safety net) records zero outbound requests to `openrouter.ai`.
4. When `LLM_FALLBACK_ENABLED=true`, the `LlmConfig` is built from the workspace's OpenRouter settings (the free-tier slugs in `project_openrouter_models.md`). The config is not constructed if the feature is disabled, so missing OpenRouter env vars are a non-issue when LLM is off.
5. `/health` does **not** go through the slowquery middleware — or if it does, its query (if any) is filtered out by the middleware's internal ignore list. We don't want platform probes polluting the fingerprint table.
6. `/version` and `/_slowquery/*` endpoints are also filtered from capture for the same reason.
7. The `PostgresStoreWriter` is stopped gracefully in a FastAPI shutdown event handler (`app.on_event("shutdown")`).

## Test cases

**Success (unit — FastAPI `TestClient` with middleware installed):**
1. After `create_app()`, `app.state._slowquery_installed is True`.
2. `/_slowquery/queries` returns 200 (the dashboard router is mounted) — the body is an empty list immediately after startup.
3. Calling `install_slowquery(app, engine, settings)` a second time does not register the middleware twice (checked by counting the number of middleware in the stack).
4. With `LLM_FALLBACK_ENABLED=false`, `install()` receives `enable_llm=False, llm_config=None` (asserted via patching the library's `install`).
5. With `LLM_FALLBACK_ENABLED=true`, `install()` receives an `LlmConfig` whose `base_url` matches `OPENROUTER_BASE_URL` and whose `primary_model` matches `OPENROUTER_MODEL_PRIMARY`.
6. A `GET /health` does not result in a new row in `query_fingerprints` (verified against a Testcontainers db with the schema applied).
7. A `GET /_slowquery/queries` does not result in a new row in `query_fingerprints`.

**Success (integration — real Postgres, real middleware, mocked OpenRouter via respx):**
8. Hitting an endpoint that issues a `SELECT * FROM orders WHERE user_id = $1` results in a new row in `query_fingerprints` with the parameterized SQL, within 500ms of the request returning.
9. With `LLM_FALLBACK_ENABLED=true` and a plan that no rule matches (synthesized), the OpenRouter client is called exactly once and a `suggestions` row with `source = 'llm'` appears.
10. Shutting down the app via `TestClient.__exit__` triggers `PostgresStoreWriter.close()` (verified via patched method spy).

**Failure / negative:**
11. `SLOWQUERY_THRESHOLD_MS=0` is rejected by Pydantic settings validation (must be > 0).
12. `SLOWQUERY_SAMPLE_RATE=1.5` is rejected by Pydantic settings validation (must be in [0, 1]).
13. `LLM_FALLBACK_ENABLED=true` with `OPENROUTER_API_KEY` unset raises `ConfigError` at startup with a clear message pointing at the missing env var.
14. `install_slowquery` being called before the engine is ready raises a typed `ConfigError`, not an `AttributeError`.

**Security / destructive-guard:**
15. The dashboard router is mounted at `/_slowquery`, not the root. A test asserts that none of the library's endpoints are visible at top-level paths (e.g. `/queries` returns 404, not a list).
16. `/_slowquery/queries/{id}/apply` — if the library exposes an "apply DDL" endpoint — is gated by `DEMO_MODE=false` → return 403. Demo environments that accept any token must still refuse to execute arbitrary DDL. (Final gating lives in the library; this test proves the demo doesn't inadvertently unlock it.)
17. With `LLM_FALLBACK_ENABLED=false`, zero outbound HTTP requests to `openrouter.ai` are made during the full integration-test suite. Enforced by a respx assertion in `conftest.py`.

## Acceptance

- [ ] `src/slowquery_demo/core/observability.py` holds `install_slowquery` and the private `_build_llm_config`.
- [ ] `src/slowquery_demo/main.py` calls `install_slowquery(app, engine, settings)` exactly once from `create_app()`.
- [ ] `src/slowquery_demo/core/config.py`'s `Settings` has typed fields for every env var listed above with Pydantic validation.
- [ ] The integration test suite runs with `LLM_FALLBACK_ENABLED=false` by default and `respx` as a safety net blocks any unexpected outbound HTTP.
- [ ] Every test case above has a corresponding test.
