"""slowquery-detective 0.2 wiring (spec 15).

The library now owns what this module used to patch in (DEVIATIONS §8,
closed): lifespan wiring, the Postgres store (schema ``slowquery``),
execution-context timing, real-parameter EXPLAIN, and the two read routes
``GET /_slowquery/queries`` and ``GET /_slowquery/queries/{id}``. What is
left here is configuration: the service id, the store URL, the LLM config,
and the ``authorize`` hook that puts those routes under platform contract v2.
"""

from __future__ import annotations

from typing import Final

from fastapi import FastAPI, Request
from pydantic import HttpUrl, SecretStr
from slowquery_detective import LlmConfig, install
from sqlalchemy.ext.asyncio import AsyncEngine

from slowquery_demo.core.config import Settings
from slowquery_demo.core.errors import ConfigError

SERVICE_ID: Final = "slowquery"
MOUNT_PATH: Final = "/_slowquery"


async def platform_admitted(request: Request) -> bool:
    """The library's ``authorize`` hook: did the contract-v2 middleware admit this request?

    ``core/platform_auth.py`` sets ``request.state.platform_admitted`` for a
    valid bastion or slowquery-frontend token, and for a tokenless request
    that ``DEMO_MODE`` lets through. Anything else, including a request that
    somehow reached the route without passing the middleware, is refused.
    """
    return getattr(request.state, "platform_admitted", False) is True


def build_llm_config(settings: Settings) -> LlmConfig:
    """The OpenRouter fallback's config; refuses to start without a key."""
    if not settings.openrouter_api_key:
        raise ConfigError("LLM_FALLBACK_ENABLED=true requires OPENROUTER_API_KEY to be set")
    return LlmConfig(
        enabled=True,
        api_key=SecretStr(settings.openrouter_api_key),
        base_url=HttpUrl(settings.openrouter_base_url),
        model_primary=settings.openrouter_model_primary,
        model_fast=settings.openrouter_model_fast,
        model_fallback=settings.openrouter_model_fallback,
    )


def install_slowquery(app: FastAPI, engine: AsyncEngine, settings: Settings) -> None:
    """Install slowquery-detective on the commerce engine. Call after the host middleware."""
    llm_config = build_llm_config(settings) if settings.llm_fallback_enabled else None
    install(
        app,
        engine,
        service=SERVICE_ID,
        threshold_ms=settings.slowquery_threshold_ms,
        sample_rate=settings.slowquery_sample_rate,
        store_url=settings.slowquery_store_url or settings.database_url,
        authorize=platform_admitted,
        mount_path=MOUNT_PATH,
        enable_llm=llm_config is not None,
        llm_config=llm_config,
    )
    app.state.llm_config = llm_config
