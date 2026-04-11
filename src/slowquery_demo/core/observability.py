"""``install_slowquery(app, engine, settings)`` — 4-line integration.

Wraps :func:`slowquery_detective.install` plus the dashboard router
mount. All env-driven configuration is read from :class:`Settings`
so tests can monkeypatch the environment and construct fresh apps
without passing config around by hand.

Four library compatibility workarounds live here and are documented
inline:

1. **StoreWriter injection.** slowquery-detective v0.1.0 constructs
   its abstract ``StoreWriter(store_url)`` inside ``install()`` —
   there's no parameter to inject a concrete subclass. We replace
   ``slowquery_detective.middleware.StoreWriter`` with
   :class:`PostgresStoreWriter` before calling install so the
   library instantiates our concrete writer.

2. **Lifespan vs. add_event_handler.** Library 0.1.0 calls
   ``app.add_event_handler("startup", ...)`` which Starlette 1.0
   removed. A module-level shim stubs that method as a no-op so
   install() completes. The actual worker lifecycle is driven by a
   FastAPI lifespan context manager defined in this module and
   passed to ``FastAPI(lifespan=...)`` in ``main.create_app()``.

3. **cursor.info → setattr(context).** Library 0.1.0's
   ``hooks.attach`` stashes per-statement start time on
   ``cursor.info[...]``, which doesn't exist on SQLAlchemy's
   ``AsyncAdapt_asyncpg_cursor``; ``context.info`` also doesn't
   exist on ``PGExecutionContext_asyncpg``. Since
   ``ExecutionContext`` has no ``__slots__`` we stash the start
   time as a plain attribute via ``setattr``.

4. **Sync-hook to async-store bridge + direct EXPLAIN.** Library
   0.1.0's ``hooks.attach`` only writes to an in-memory
   ``RingBuffer`` for rolling percentile stats — nothing bridges
   the hook to the ``StoreWriter`` so ``query_fingerprints``,
   ``query_samples``, ``explain_plans``, and ``suggestions`` never
   get any rows. The library's ``ExplainWorker`` would run
   ``EXPLAIN`` for us, but its ``synthesize_params`` helper
   produces invalid SQL for parameterised queries
   (``where user_id = cast(1 as uuid)``, ``limit cast(now() as
   int)``) and every EXPLAIN attempt fails silently.

   Shim 4 solves both problems with one mechanism: a small
   sync-to-async queue populated from the hook via
   ``loop.call_soon_threadsafe`` that carries the **actual**
   statement and parameters, plus a background drainer task that
   runs under the FastAPI lifespan. The drainer calls
   ``store.upsert_fingerprint`` + ``store.record_sample`` for
   every observed query and, for any query over ``threshold_ms``,
   runs ``EXPLAIN (FORMAT JSON)`` directly through asyncpg using
   the real captured parameters — then feeds the resulting plan
   into the library's ``run_rules`` for suggestions. The library's
   own ``ExplainWorker`` is still started (lifecycle) but its
   queue is never pushed to.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from slowquery_detective.rules import run_rules
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

# Shim 1: Starlette 1.0 removed ``add_event_handler``.
if not hasattr(Starlette, "add_event_handler"):

    def _compat_add_event_handler(
        self: Starlette,
        event_type: str,
        func: Any,
    ) -> None:
        _ = (self, event_type, func)

    Starlette.add_event_handler = _compat_add_event_handler  # type: ignore[attr-defined]


# Shim 2: StoreWriter module-level swap.
setattr(_sqd_middleware, "StoreWriter", PostgresStoreWriter)  # noqa: B010


# Shim 3: Replace the library's ``hooks.attach`` with a version that
# stashes per-statement start time as an attribute on the
# ``ExecutionContext`` (since ``cursor.info`` doesn't exist on async
# cursors and ``context.info`` doesn't exist on asyncpg contexts).
# The patched ``attach`` also emits a ``(fp_id, canonical_sql,
# duration_ms)`` record onto a sync-to-async bridge queue so shim 4
# can persist it.
_CONTEXT_START_ATTR = "_slowquery_demo_start"


# Bridge tuple shape:
#   (fingerprint_id, canonical_sql, raw_statement, raw_parameters, duration_ms)
#
# ``raw_statement`` is what SQLAlchemy sent to the driver (already in
# asyncpg ``$1, $2`` form for parameterised queries) and
# ``raw_parameters`` is the positional tuple used at execute time.
# Both are needed so the drainer can run a real EXPLAIN via asyncpg
# without fighting ``synthesize_params``' broken UUID / limit guesses.
_BridgeItem = tuple[str, str, str, tuple[Any, ...], float]


def _make_patched_attach(
    bridge_queue: asyncio.Queue[_BridgeItem],
    loop_ref: list[asyncio.AbstractEventLoop | None],
) -> Any:
    """Build a ``hooks.attach`` replacement closed over the bridge queue."""

    def _patched_attach(
        engine: Any,
        buffer: RingBuffer,
        *,
        sample_rate: float = 1.0,
    ) -> None:
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
            _ = (conn, cursor, executemany)
            start = getattr(context, _CONTEXT_START_ATTR, None)
            if start is None:
                return
            duration_ms = (time.perf_counter() - start) * 1000.0
            try:
                fp_id, canonical_sql = fingerprint_fn(statement)
            except Exception:
                _LOG.debug("slowquery.hooks.fingerprint_skipped", exc_info=True)
                return
            try:
                buffer.record(fp_id, duration_ms)
            except Exception:
                _LOG.exception("slowquery.hooks.record_failed")

            # Normalise parameters to a positional tuple. SQLAlchemy's
            # asyncpg dialect passes a tuple/list of positional args;
            # other dialects may pass a dict. We only care about
            # positional here since we're pairing with ``$1, $2, ...``.
            if isinstance(parameters, list | tuple):
                params_tuple = tuple(parameters)
            elif isinstance(parameters, dict):
                params_tuple = tuple(parameters.values())
            else:
                params_tuple = ()

            item: _BridgeItem = (
                fp_id,
                canonical_sql,
                statement,
                params_tuple,
                duration_ms,
            )

            loop = loop_ref[0]
            if loop is None or loop.is_closed():
                return
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(_try_put_nowait, bridge_queue, item)

        event.listen(sync_engine, "before_cursor_execute", _before)
        event.listen(sync_engine, "after_cursor_execute", _after)
        sync_engine._slowquery_listeners = (_before, _after)
        sync_engine._slowquery_attached = True

    return _patched_attach


def _try_put_nowait(
    queue: asyncio.Queue[_BridgeItem],
    item: _BridgeItem,
) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # If the drainer is backed up, dropping the oldest sample is
        # better than blocking a request handler.
        try:
            queue.get_nowait()
            queue.put_nowait(item)
        except (asyncio.QueueFull, asyncio.QueueEmpty):
            pass


# Module-level bridge queue (bounded to cap memory) and a mutable
# loop reference populated when the FastAPI lifespan starts. Both are
# module-level so the patched attach closure can reach them without
# having to re-patch at install() time.
_BRIDGE_QUEUE: asyncio.Queue[_BridgeItem] = asyncio.Queue(maxsize=10_000)
_LOOP_REF: list[asyncio.AbstractEventLoop | None] = [None]

_patched_attach = _make_patched_attach(_BRIDGE_QUEUE, _LOOP_REF)
setattr(_sqd_hooks, "attach", _patched_attach)  # noqa: B010
setattr(_sqd_middleware, "attach", _patched_attach)  # noqa: B010


async def _drainer(app: FastAPI) -> None:
    """Background task that consumes the bridge queue.

    For every bridge item, the drainer:

    1. Upserts the fingerprint (bumps call_count, refreshes last_seen).
    2. Records a sample (keeps rolling percentile stats fresh).
    3. If the sample exceeds ``threshold_ms`` AND the fingerprint is
       outside its per-fingerprint cooldown window, runs
       ``EXPLAIN (FORMAT JSON)`` against the real captured statement
       + parameters through the store's asyncpg pool, then feeds the
       plan to the library's ``run_rules`` for suggestions. Plan +
       suggestions get written through the same store writer.
    """
    store = app.state.slowquery_store
    threshold_ms = app.state.slowquery_threshold_ms

    # Per-fingerprint cooldown — run at most one EXPLAIN per fingerprint
    # per minute so a traffic burst doesn't swamp Neon.
    cooldown: dict[str, float] = {}
    cooldown_seconds = 60.0

    while True:
        try:
            (
                fp_id,
                canonical_sql,
                raw_statement,
                raw_parameters,
                duration_ms,
            ) = await _BRIDGE_QUEUE.get()
        except asyncio.CancelledError:
            return

        try:
            await store.upsert_fingerprint(fp_id, canonical_sql)
        except Exception:
            _LOG.exception("slowquery.drainer.upsert_fingerprint_failed")
            continue

        try:
            await store.record_sample(fp_id, duration_ms=duration_ms, rows=None)
        except Exception:
            _LOG.exception("slowquery.drainer.record_sample_failed")

        if duration_ms < threshold_ms:
            continue

        now = time.monotonic()
        if cooldown.get(fp_id, 0) > now:
            continue

        plan = await _run_direct_explain(store, raw_statement, raw_parameters)
        if plan is None:
            cooldown[fp_id] = now + cooldown_seconds
            continue

        try:
            suggestions = run_rules(plan, canonical_sql, fingerprint_id=fp_id)
        except Exception:
            _LOG.exception("slowquery.drainer.rules_failed")
            suggestions = []

        cost = 0.0
        plan_root = plan.get("Plan") if isinstance(plan, dict) else None
        if isinstance(plan_root, dict):
            cost = float(plan_root.get("Total Cost") or 0.0)

        try:
            await store.upsert_plan(fp_id, plan_json=plan, plan_text=json.dumps(plan), cost=cost)
        except Exception:
            _LOG.exception("slowquery.drainer.upsert_plan_failed")

        if suggestions:
            try:
                await store.insert_suggestions(fp_id, suggestions)
            except Exception:
                _LOG.exception("slowquery.drainer.insert_suggestions_failed")

        cooldown[fp_id] = now + cooldown_seconds


async def _run_direct_explain(
    store: Any,
    raw_statement: str,
    raw_parameters: tuple[Any, ...],
) -> dict[str, Any] | None:
    """Run ``EXPLAIN (FORMAT JSON) <statement>`` via the store's asyncpg pool.

    Uses the real captured parameters so there's no
    ``synthesize_params`` guessing. Returns the top-level plan dict or
    ``None`` on failure.
    """
    try:
        pool = await store._ensure_pool()
    except Exception:
        _LOG.exception("slowquery.drainer.pool_unavailable")
        return None

    explain_sql = f"EXPLAIN (FORMAT JSON) {raw_statement}"
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval(explain_sql, *raw_parameters)
    except Exception:
        _LOG.debug("slowquery.drainer.explain_error", exc_info=True)
        return None

    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
    else:
        parsed = raw

    if isinstance(parsed, list) and parsed:
        first = parsed[0]
        return first if isinstance(first, dict) else None
    if isinstance(parsed, dict):
        return parsed
    return None


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
    """FastAPI lifespan: starts the library worker + our drainer task.

    Records the running event loop on module-level ``_LOOP_REF`` so the
    sync attach hook can dispatch to the drainer via
    ``loop.call_soon_threadsafe``.
    """
    _LOOP_REF[0] = asyncio.get_running_loop()

    worker = getattr(app.state, "slowquery_worker", None)
    if worker is not None:
        await worker.start()

    drainer_task: asyncio.Task[None] | None = None
    if getattr(app.state, "slowquery_store", None) is not None and worker is not None:
        drainer_task = asyncio.create_task(_drainer(app), name="slowquery_demo_drainer")

    try:
        yield
    finally:
        if drainer_task is not None:
            drainer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await drainer_task
        if worker is not None:
            await worker.stop()
        store = getattr(app.state, "slowquery_store", None)
        if store is not None and hasattr(store, "close"):
            try:
                await store.close()
            except Exception:
                _LOG.exception("slowquery.lifespan.store_close_failed")
        _LOOP_REF[0] = None
