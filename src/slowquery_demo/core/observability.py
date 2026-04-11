"""``install_slowquery(app, engine, settings)`` — 4-line integration.

Wraps :func:`slowquery_detective.install` plus the dashboard router
mount. All env-driven configuration is read from :class:`Settings`
so tests can monkeypatch the environment and construct fresh apps
without passing config around by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import SecretStr
from slowquery_detective import install
from slowquery_detective.llm_explainer import LlmConfig
from starlette.applications import Starlette

from slowquery_demo.api.routers.dashboard import router as dashboard_router
from slowquery_demo.core.errors import ConfigError

if TYPE_CHECKING:
    from fastapi import FastAPI

    from slowquery_demo.core.config import Settings


# --- Starlette 1.0 compatibility shim ------------------------------------
# slowquery-detective v0.1.0 calls ``app.add_event_handler("startup", ...)``
# inside its ``install()``. Starlette 1.0 removed that API in favour of
# the lifespan context manager. Until the library publishes a patched
# release that uses lifespan, stub ``add_event_handler`` as a no-op so
# ``install()`` completes. Integration tests in S5 will wire the
# slowquery-detective worker lifecycle via the FastAPI lifespan handler
# instead of relying on this shim.
if not hasattr(Starlette, "add_event_handler"):

    def _compat_add_event_handler(
        self: Starlette,
        event_type: str,
        func: Any,
    ) -> None:
        _ = (self, event_type, func)  # intentionally unused

    Starlette.add_event_handler = _compat_add_event_handler  # type: ignore[attr-defined]


_INSTALLED_ATTR = "_slowquery_installed"


def install_slowquery(
    app: FastAPI,
    engine: Any,
    settings: Settings | None,
) -> None:
    """Wire slowquery-detective into ``app`` + mount the dashboard router.

    Raises:
        ConfigError: if ``engine`` or ``settings`` is None, or if LLM
            fallback is enabled without an OpenRouter API key.
    """
    if engine is None:
        raise ConfigError("install_slowquery: engine is None (call build_engine first)")
    if settings is None:
        raise ConfigError("install_slowquery: settings is None")

    if getattr(app.state, _INSTALLED_ATTR, False):
        return

    llm_config = _build_llm_config(settings) if settings.llm_fallback_enabled else None

    install(
        app,
        engine,
        threshold_ms=settings.slowquery_threshold_ms,
        sample_rate=settings.slowquery_sample_rate,
        store_url=settings.slowquery_store_url or settings.database_url,
        enable_llm=settings.llm_fallback_enabled,
        llm_config=llm_config,
    )
    app.include_router(dashboard_router, prefix="/_slowquery")
    app.state._slowquery_installed = True


def _build_llm_config(settings: Settings) -> LlmConfig:
    if not settings.openrouter_api_key:
        raise ConfigError("LLM_FALLBACK_ENABLED=true requires OPENROUTER_API_KEY to be set")
    return LlmConfig(
        enabled=True,
        api_key=SecretStr(settings.openrouter_api_key),
        base_url=settings.openrouter_base_url,  # type: ignore[arg-type]
        model_primary=settings.openrouter_model_primary,
        model_fast=settings.openrouter_model_fast,
        model_fallback=settings.openrouter_model_fallback,
    )
