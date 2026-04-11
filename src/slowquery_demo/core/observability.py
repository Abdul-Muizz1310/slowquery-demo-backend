"""``install_slowquery(app, engine, settings)`` — 4-line integration.

Wraps :func:`slowquery_detective.install` plus the dashboard router
mount. All env-driven configuration is read from :class:`Settings`
so tests can monkeypatch the environment and construct fresh apps
without passing config around by hand.

Three library compatibility workarounds live here and are
documented inline:

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

3. **cursor.info vs. context.info in hooks.** Library 0.1.0's
   ``hooks.attach`` stashes per-statement start time on
   ``cursor.info[...]``, which doesn't exist on SQLAlchemy's
   ``AsyncAdapt_asyncpg_cursor`` adapter — the first real DB
   query through the async engine raises ``AttributeError:
   'AsyncAdapt_asyncpg_cursor' object has no attribute 'info'``.
   The SQLAlchemy event callback signature is
   ``(conn, cursor, statement, parameters, context, executemany)``
   and ``context.info`` is a plain dict available on both sync
   and async paths, so we replace the library's ``attach`` with a
   wrapper that uses ``context.info[...]`` instead.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import slowquery_detective.hooks as _sqd_hooks
import slowquery_detective.middleware as _sqd_middleware
from pydantic import SecretStr
from slowquery_detective import install
from slowquery_detective.buffer import RingBuffer
from slowquery_detective.fingerprint import fingerprint as fingerprint_fn
from slowquery_detective.llm_explainer import LlmConfig
from sqlalchemy import event
from starlette.applications import Starlette

from slowquery_demo.api.routers.dashboard import router as dashboard_router
from slowquery_demo.core.errors import ConfigError
from slowquery_demo.services.store import PostgresStoreWriter

if TYPE_CHECKING:
    from fastapi import FastAPI

    from slowquery_demo.core.config import Settings


_LOG = logging.getLogger("slowquery_demo.observability")


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


# Shim 3: Replace the library's ``hooks.attach`` with a version that
# stashes per-statement start time as a regular attribute on the
# SQLAlchemy ``ExecutionContext`` via ``setattr``. The library's version
# used ``cursor.info[...]`` which doesn't exist on
# ``AsyncAdapt_asyncpg_cursor``; ``context.info`` also doesn't exist
# on ``PGExecutionContext_asyncpg``. ``ExecutionContext`` has no
# ``__slots__`` so a plain attribute survives the full
# ``before`` -> ``after`` round-trip without any mapping gymnastics.
_CONTEXT_START_ATTR = "_slowquery_demo_start"


def _patched_attach(
    engine: Any,
    buffer: RingBuffer,
    *,
    sample_rate: float = 1.0,
) -> None:
    """Drop-in replacement for slowquery_detective.hooks.attach."""
    if engine is None:
        raise ValueError("engine must not be None")
    if buffer is None:
        raise ValueError("buffer must not be None")
    if not 0.0 <= sample_rate <= 1.0:
        raise ValueError("sample_rate must be in [0.0, 1.0]")

    sync_engine = engine.sync_engine if hasattr(engine, "sync_engine") else engine

    if getattr(sync_engine, "_slowquery_attached", False):
        return

    rng = random.Random(id(sync_engine))

    def _before(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        _ = (conn, cursor, parameters, statement, executemany)
        if sample_rate < 1.0 and rng.random() >= sample_rate:
            setattr(context, _CONTEXT_START_ATTR, None)
            return
        setattr(context, _CONTEXT_START_ATTR, time.perf_counter())

    def _after(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        _ = (conn, cursor, parameters, executemany)
        start = getattr(context, _CONTEXT_START_ATTR, None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start) * 1000.0
        try:
            fp_id, _ = fingerprint_fn(statement)
        except Exception:
            _LOG.debug("slowquery.hooks.fingerprint_skipped", exc_info=True)
            return
        try:
            buffer.record(fp_id, duration_ms)
        except Exception:
            _LOG.exception("slowquery.hooks.record_failed")

    event.listen(sync_engine, "before_cursor_execute", _before)
    event.listen(sync_engine, "after_cursor_execute", _after)
    sync_engine._slowquery_listeners = (_before, _after)
    sync_engine._slowquery_attached = True


# Swap the library's attach for the patched version. The library's
# install() imports ``attach`` by name from its own module, so we
# patch on the module object itself.
setattr(_sqd_hooks, "attach", _patched_attach)  # noqa: B010
setattr(_sqd_middleware, "attach", _patched_attach)  # noqa: B010


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
