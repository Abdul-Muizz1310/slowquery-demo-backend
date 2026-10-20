"""Environment-driven configuration for slowquery_demo."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All service configuration lives here.

    Every env var from ``.env.example`` has a typed field with validation.
    Missing / malformed values raise :class:`pydantic.ValidationError` at
    construction time so operators see failures at startup, not at first
    request.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- runtime ---
    app_env: str = "development"
    log_level: str = "info"
    port: int = 8000
    # Contract v2 (spec 14): DEMO_MODE only lets a request *without* a token
    # through anonymous; an invalid token is still 401. Off unless set.
    demo_mode: bool = False
    cors_origins: str = ""

    # --- platform token keys (spec 14) ---
    # bastion's Ed25519 public key (base64 SPKI DER), else fetched from the URL
    # (JSON field ``publicKey``, cached 1 h); and the dashboard frontend's own.
    bastion_signing_key_public: str | None = None
    bastion_public_key_url: str | None = None
    slowquery_frontend_signing_key_public: str | None = None

    # --- mutation gating (public-URL hardening) ---
    # Shared secret required to call the state-mutating endpoints
    # (POST /branches/switch, POST /_slowquery/*/force-explain) even
    # while DEMO_MODE bypasses the platform-token middleware. When unset,
    # the destructive force-explain endpoint fails closed (403) so an
    # anonymous visitor can never overwrite a genuine captured plan.
    demo_mutation_token: str | None = None
    # Per-client cooldown (seconds) enforced on mutating endpoints even in
    # demo mode. 0 disables the cooldown (the default keeps unit tests
    # deterministic); production sets a positive value via env.
    demo_mutation_cooldown_s: float = Field(default=0.0, ge=0.0)

    # --- database ---
    # Defaults are a localhost dummy URL so ``create_app()`` can build an
    # AsyncEngine without a live Postgres; unit tests override ``get_db``
    # via dependency_overrides so the engine is never actually dialed.
    database_url: str = "postgresql+asyncpg://test:test@localhost/test_slowquery"
    database_url_fast: str = "postgresql+asyncpg://test:test@localhost/test_slowquery_fast"
    branch_current: str = "slow"

    # --- slowquery-detective tunables ---
    slowquery_threshold_ms: int = Field(default=100, gt=0)
    slowquery_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    # Where slowquery-detective keeps its tables (schema ``slowquery``);
    # DATABASE_URL (the slow branch) when unset. Retention, batching and
    # percentiles are the library's (spec 15); the 0.1-era drainer knobs
    # SLOWQUERY_SAMPLE_RETENTION_S / SLOWQUERY_STATS_RECOMPUTE_INTERVAL_S are
    # gone and ignored if still set.
    slowquery_store_url: str | None = None

    # --- LLM fallback (OpenRouter) ---
    llm_fallback_enabled: bool = False
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # The portfolio's free OpenRouter models; never a paid default.
    openrouter_model_primary: str = "google/gemma-4-26b-a4b-it:free"
    openrouter_model_fast: str = "minimax/minimax-m3.1-flash:free"
    openrouter_model_fallback: str = "google/gemma-4-31b-it:free"

    # NOTE: there are deliberately no NEON_API_KEY / NEON_PROJECT_ID fields.
    # Branch switching never calls the Neon API — spec 06 rules that out
    # explicitly ("calling out to the Neon API from an HTTP handler is a
    # latency hazard and a secrets-leak hazard"). A switch is a pure
    # URL + engine swap between DATABASE_URL and DATABASE_URL_FAST
    # (services/branch_switcher.py + main._make_engine_builder). Those two
    # settings used to be declared here and read by nothing, which advertised
    # a mechanism that does not exist. ``extra="ignore"`` above means a stale
    # NEON_* value left in an environment is simply ignored, not an error.


def get_settings() -> Settings:
    """Return a fresh :class:`Settings` instance.

    Not memoised — tests monkeypatch env vars and need a fresh read.
    Production callers should cache the return value at startup.
    """
    return Settings()
