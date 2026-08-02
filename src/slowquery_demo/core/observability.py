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
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

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


# --- Self-instrumentation ignore list (spec 05 invariants 5-6) -----------
#
# The dashboard read API (``api/routers/dashboard.py`` →
# ``repositories/slowquery_repository.py``) selects from the bookkeeping
# tables through the *same* instrumented engine that serves the commerce
# routes, and ``/health`` probes that engine with ``SELECT 1``. Without a
# filter the pipeline observes itself: every dashboard poll and every
# readiness probe becomes a permanent ``query_fingerprints`` row, and
# because ``total_ms`` accumulates on every call the observability
# system's own bookkeeping reads climb to the top of the demo's headline
# ``/_slowquery/queries`` list — burying the seeded commerce slow queries
# the demo exists to show.

# The four bookkeeping tables written by :class:`PostgresStoreWriter`.
IGNORED_TABLES: Final[frozenset[str]] = frozenset(
    {"query_fingerprints", "query_samples", "explain_plans", "suggestions"}
)

# Word-boundary alternation so ``suggestions_count`` / ``archived_suggestions``
# on a commerce table are still recorded. Sorted for a stable pattern.
_IGNORED_TABLE_RE: Final = re.compile(
    r"\b(?:" + "|".join(sorted(IGNORED_TABLES)) + r")\b", re.IGNORECASE
)

# The readiness probe ``platform._health`` (and ``main._make_engine_builder``'s
# post-rebuild health check) issue verbatim. Anchored so ``SELECT 100`` and
# ``SELECT 1, orders.id ...`` are not swallowed.
_HEALTH_PROBE_RE: Final = re.compile(r"^select\s+1\s*;?$", re.IGNORECASE)


def should_ignore_statement(statement: str) -> bool:
    """True when ``statement`` is the observability system's own traffic.

    Applied in the ``after_cursor_execute`` hook *before* the rolling
    buffer and the bridge queue, so an ignored statement produces neither
    a percentile sample nor a persisted fingerprint.

    Ignored:

    - any statement referencing one of :data:`IGNORED_TABLES` (the
      dashboard read API's own bookkeeping selects),
    - the ``SELECT 1`` readiness probe behind ``/health``,
    - a blank statement (nothing meaningful to fingerprint).

    Everything else — the seeded commerce queries the demo exists to
    surface — is recorded.
    """
    collapsed = " ".join(statement.split())
    if not collapsed:
        return True
    if _HEALTH_PROBE_RE.match(collapsed):
        return True
    return _IGNORED_TABLE_RE.search(collapsed) is not None


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
            # Spec 05 invariants 5-6: never observe our own bookkeeping
            # reads or the readiness probe. Checked before both recording
            # sinks so an ignored statement leaves no trace at all.
            if should_ignore_statement(statement):
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


@dataclass
class _DrainState:
    """Mutable per-drainer bookkeeping + tunables.

    Extracting this (and :func:`_drain_one`) out of the ``while True``
    loop lets the two behaviours the audit flagged — the per-fingerprint
    percentile-recompute throttle (OPT-1) and the periodic retention
    prune (COST-1) — be unit-tested on the real drain path with an
    injected clock, instead of living in an untestable infinite loop.
    """

    threshold_ms: float
    # query_samples retention (seconds); <= 0 disables the periodic prune.
    retention_s: float
    # Minimum seconds between full percentile recomputes per fingerprint;
    # <= 0 recomputes on every sample (legacy behaviour).
    stats_interval: float
    # How often (seconds of drainer runtime) the retention prune fires.
    prune_interval_s: float = 300.0
    # Per-fingerprint EXPLAIN cooldown so a burst doesn't swamp Neon.
    cooldown_seconds: float = 60.0

    cooldown: dict[str, float] = field(default_factory=dict)
    last_stats: dict[str, float] = field(default_factory=dict)
    last_prune: float | None = None


async def _drain_one(
    store: Any,
    state: _DrainState,
    item: _BridgeItem,
    *,
    now: float | None = None,
) -> None:
    """Process a single bridge item. The real drain path for one query.

    For the item, this:

    1. Upserts the fingerprint (bumps call_count, refreshes last_seen).
    2. Records a sample. The expensive ``percentile_cont`` recompute is
       throttled per fingerprint via ``stats_interval`` (OPT-1); bursts
       within the window only bump ``total_ms``/``last_seen``.
    3. Periodically prunes ``query_samples`` older than ``retention_s``
       so the table can't grow without bound on free-tier Neon (COST-1).
    4. If the sample exceeds ``threshold_ms`` AND the fingerprint is
       outside its per-fingerprint cooldown, runs ``EXPLAIN (FORMAT
       JSON)`` through the store's asyncpg pool and feeds the plan to
       ``run_rules`` for suggestions. Plan + suggestions are persisted.

    ``now`` is injectable so the throttle/prune timing is unit-testable;
    it defaults to ``time.monotonic()`` on the live path.
    """
    now = time.monotonic() if now is None else now
    if state.last_prune is None:
        # First item seen: anchor the prune clock so a prune can't fire
        # on the very first observed query.
        state.last_prune = now

    fp_id, canonical_sql, raw_statement, raw_parameters, duration_ms = item

    try:
        await store.upsert_fingerprint(fp_id, canonical_sql)
    except Exception:
        _LOG.exception("slowquery.drainer.upsert_fingerprint_failed")
        return

    recompute = (
        state.stats_interval <= 0
        or (now - state.last_stats.get(fp_id, 0.0)) >= state.stats_interval
    )
    try:
        await store.record_sample(
            fp_id, duration_ms=duration_ms, rows=None, recompute_stats=recompute
        )
        if recompute:
            state.last_stats[fp_id] = now
    except Exception:
        _LOG.exception("slowquery.drainer.record_sample_failed")

    # Periodic retention prune to bound query_samples growth (COST-1).
    if state.retention_s > 0 and (now - state.last_prune) >= state.prune_interval_s:
        state.last_prune = now
        try:
            await store.prune_samples(state.retention_s)
        except Exception:
            _LOG.exception("slowquery.drainer.prune_samples_failed")

    if duration_ms < state.threshold_ms:
        return

    if state.cooldown.get(fp_id, 0) > now:
        return

    plan = await _run_direct_explain(store, raw_statement, raw_parameters)
    if plan is None:
        state.cooldown[fp_id] = now + state.cooldown_seconds
        return

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

    state.cooldown[fp_id] = now + state.cooldown_seconds


def _build_drain_state(app: FastAPI) -> _DrainState:
    """Construct the drainer's state from ``app.state`` + settings."""
    settings = getattr(app.state, "settings", None)
    return _DrainState(
        threshold_ms=app.state.slowquery_threshold_ms,
        retention_s=getattr(settings, "slowquery_sample_retention_s", 86_400.0),
        stats_interval=getattr(settings, "slowquery_stats_recompute_interval_s", 2.0),
    )


async def _drainer(app: FastAPI) -> None:
    """Background task that consumes the bridge queue.

    Owns the throttle/prune bookkeeping (:class:`_DrainState`) and
    delegates per-item work to :func:`_drain_one` so the composed flow
    is exercised by unit tests rather than living in an untestable loop.
    """
    store = app.state.slowquery_store
    state = _build_drain_state(app)

    while True:
        try:
            item = await _BRIDGE_QUEUE.get()
        except asyncio.CancelledError:
            return
        await _drain_one(store, state, item)


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


def reattach_slowquery(app: FastAPI) -> bool:
    """Attach the slowquery hooks to the engine currently on ``app.state``.

    The library's ``attach`` (and our patched replacement) stamps
    ``_slowquery_attached`` on the *sync* engine and short-circuits if it
    is already set. A branch switch builds a brand-new ``AsyncEngine``
    whose sync engine has no such stamp, so the listeners installed at
    startup do not follow the swap — new queries on the new engine emit
    no samples. Calling this after the swap installs fresh listeners on
    the new engine so observability resumes within one request.

    Returns ``True`` if listeners were attached, ``False`` if the
    pipeline is not installed or the buffer/engine is missing (in which
    case there is nothing to re-attach).
    """
    if not getattr(app.state, _INSTALLED_ATTR, False):
        return False
    buffer = getattr(app.state, "slowquery_buffer", None)
    engine = getattr(app.state, "engine", None)
    if buffer is None or engine is None:
        return False
    settings = getattr(app.state, "settings", None)
    sample_rate = getattr(settings, "slowquery_sample_rate", 1.0)
    _patched_attach(engine, buffer, sample_rate=sample_rate)
    return True


async def on_branch_switch(app: FastAPI) -> None:
    """Side effects the branch switcher runs after a successful swap.

    Two responsibilities, both keyed off the engine that ``main.py``
    has already swapped onto ``app.state``:

    1. **Clear the rolling buffer** (spec 06 invariant 5). Percentiles
       computed from the old branch would otherwise pollute the new
       branch's fresh stats — the dashboard's p95 line would not visibly
       drop after switching to the indexed branch.
    2. **Re-attach the hooks** to the swapped engine so new samples flow
       on the new branch (the startup listeners stay bound to the old,
       now-disposed engine).
    """
    buffer = getattr(app.state, "slowquery_buffer", None)
    if buffer is not None and hasattr(buffer, "clear"):
        buffer.clear()
    reattach_slowquery(app)


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
