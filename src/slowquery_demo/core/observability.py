"""``install_slowquery(app, engine, settings)`` — 4-line integration.

Wraps :func:`slowquery_detective.install` plus the dashboard router
mount. All env-driven configuration is read from :class:`Settings`
so tests can monkeypatch the environment and construct fresh apps
without passing config around by hand.

Two library compatibility workarounds live here and are documented
inline:

1. **StoreWriter injection.** slowquery-detective v0.1.0 constructs
   its abstract ``StoreWriter(store_url)`` inside ``install()`` —
   there's no parameter to inject a concrete subclass. We replace
   ``slowquery_detective.middleware.StoreWriter`` with
   :class:`PostgresStoreWriter` before calling install so the
   library instantiates our concrete writer. The abstract base's
   ``NotImplementedError`` bodies are never reached.

2. **Lifespan vs. add_event_handler.** Library 0.1.0 calls
   ``app.add_event_handler("startup", ...)`` which Starlette 1.0
   removed. A module-level shim stubs that method as a no-op so
   install() completes. The actual worker lifecycle is driven by a
   FastAPI lifespan context manager defined in this module and
   passed to ``FastAPI(lifespan=...)`` in ``main.create_app()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import slowquery_detective.middleware as _sqd_middleware
from pydantic import SecretStr
from slowquery_detective import install
from slowquery_detective.llm_explainer import LlmConfig
from starlette.applications import Starlette

from slowquery_demo.api.routers.dashboard import router as dashboard_router
from slowquery_demo.core.errors import ConfigError
from slowquery_demo.services.store import PostgresStoreWriter

if TYPE_CHECKING:
    from fastapi import FastAPI

    from slowquery_demo.core.config import Settings


# --- Library compatibility shims ----------------------------------------

# Shim 1: Starlette 1.0 removed ``add_event_handler``. Library 0.1.0 still
# calls it inside install(). Stub as a no-op so install() completes; the
# actual worker lifecycle is driven by the lifespan handler below.
if not hasattr(Starlette, "add_event_handler"):

    def _compat_add_event_handler(
        self: Starlette,
        event_type: str,
        func: Any,
    ) -> None:
        _ = (self, event_type, func)  # intentionally unused

    Starlette.add_event_handler = _compat_add_event_handler  # type: ignore[attr-defined]


# Shim 2: Swap the library's abstract StoreWriter for our concrete
# PostgresStoreWriter. The library's install() does
# ``store = StoreWriter(store_url or _engine_url(engine))``; replacing
# the attribute on the module before install() means that line creates
# a PostgresStoreWriter instance instead of the NotImplementedError
# base class.
setattr(_sqd_middleware, "StoreWriter", PostgresStoreWriter)  # noqa: B010


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


@asynccontextmanager
async def slowquery_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan that drives the slowquery-detective worker.

    Starts the ``ExplainWorker`` the library installed on app.state on
    startup and stops it on shutdown. The store writer owned by the
    worker is also closed on shutdown so the asyncpg pool drains
    cleanly.
    """
    worker = getattr(app.state, "slowquery_worker", None)
    if worker is not None:
        await worker.start()
    try:
        yield
    finally:
        if worker is not None:
            await worker.stop()
        store = getattr(app.state, "slowquery_store", None)
        if store is not None and hasattr(store, "close"):
            await store.close()
