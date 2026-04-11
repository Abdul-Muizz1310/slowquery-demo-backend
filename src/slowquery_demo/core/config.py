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
    demo_mode: bool = True
    cors_origins: str = ""

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
    slowquery_store_url: str | None = None

    # --- LLM fallback (OpenRouter) ---
    llm_fallback_enabled: bool = False
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model_primary: str = ""
    openrouter_model_fast: str = ""
    openrouter_model_fallback: str = ""

    # --- Neon API (branch switching) ---
    neon_api_key: str | None = None
    neon_project_id: str | None = None


def get_settings() -> Settings:
    """Return a fresh :class:`Settings` instance.

    Not memoised — tests monkeypatch env vars and need a fresh read.
    Production callers should cache the return value at startup.
    """
    return Settings()
