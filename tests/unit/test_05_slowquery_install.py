"""S3 red: unit tests for spec 05 (slowquery-detective install + mount)."""

from __future__ import annotations

from unittest.mock import patch

import pytest


def test_create_app_flags_installed() -> None:
    """Spec 05 test 1."""
    from slowquery_demo.main import create_app

    app = create_app()
    assert getattr(app.state, "_slowquery_installed", False) is True


def test_dashboard_router_mounted_at_underscore_slowquery(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 2."""
    resp = test_client.get("/_slowquery/queries")
    assert resp.status_code == 200
    assert resp.json() == []


def test_install_is_idempotent() -> None:
    """Spec 05 test 3."""
    from slowquery_demo.core.observability import install_slowquery
    from slowquery_demo.main import create_app

    app = create_app()
    before = len(app.user_middleware)
    install_slowquery(app, app.state.engine, app.state.settings)
    after = len(app.user_middleware)
    assert before == after


def test_llm_disabled_passes_no_config() -> None:
    """Spec 05 test 4."""
    from slowquery_demo.core.observability import install_slowquery
    from slowquery_demo.main import create_app

    with patch("slowquery_demo.core.observability.install") as mock_install:
        app = create_app()
        install_slowquery(app, app.state.engine, app.state.settings)
    # The first positional is app, second is engine.
    kwargs = mock_install.call_args.kwargs
    assert kwargs["enable_llm"] is False
    assert kwargs["llm_config"] is None


def test_llm_enabled_builds_llm_config(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 5."""
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("OPENROUTER_MODEL_PRIMARY", "meta-llama/llama-3.3-70b")

    from slowquery_demo.core.observability import install_slowquery
    from slowquery_demo.main import create_app

    with patch("slowquery_demo.core.observability.install") as mock_install:
        app = create_app()
        install_slowquery(app, app.state.engine, app.state.settings)

    kwargs = mock_install.call_args.kwargs
    assert kwargs["enable_llm"] is True
    assert kwargs["llm_config"] is not None
    # LlmConfig.base_url is a pydantic HttpUrl; compare via str-cast.
    assert str(kwargs["llm_config"].base_url).rstrip("/") == "https://openrouter.ai/api/v1"


# Spec 05 tests 6 and 7 (health/_slowquery not captured) need a real
# pg_engine fixture and live in tests/integration/test_05_slowquery_install.py.


def test_threshold_ms_zero_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 11."""
    monkeypatch.setenv("SLOWQUERY_THRESHOLD_MS", "0")
    from pydantic import ValidationError

    from slowquery_demo.core.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_sample_rate_out_of_range_rejected(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 12."""
    monkeypatch.setenv("SLOWQUERY_SAMPLE_RATE", "1.5")
    from pydantic import ValidationError

    from slowquery_demo.core.config import Settings

    with pytest.raises(ValidationError):
        Settings()


def test_llm_enabled_without_api_key_raises_config_error(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 13."""
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "true")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from slowquery_demo.core.errors import ConfigError
    from slowquery_demo.main import create_app

    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        create_app()


def test_install_before_engine_ready_raises_typed_error() -> None:
    """Spec 05 test 14."""
    from fastapi import FastAPI

    from slowquery_demo.core.errors import ConfigError
    from slowquery_demo.core.observability import install_slowquery

    app = FastAPI()
    with pytest.raises(ConfigError):
        install_slowquery(app, engine=None, settings=None)  # type: ignore[arg-type]


def test_no_toplevel_queries_endpoint(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 15."""
    resp = test_client.get("/queries")
    assert resp.status_code == 404


# Spec 05 test 16 (DEMO_MODE=false rejects apply-DDL) needs a non-demo-mode
# app variant and lives in tests/integration/test_05_slowquery_install.py.


def test_no_outbound_openrouter_when_llm_disabled(test_client, respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 17."""
    from httpx import Response

    openrouter = respx_mock.route(host="openrouter.ai").mock(
        return_value=Response(500, json={"err": "should not be called"})
    )
    test_client.get("/users?limit=5")
    assert not openrouter.called


# --- Branch-switch side effects (spec 06 invariant 5) -------------------


@pytest.mark.asyncio
async def test_on_branch_switch_clears_buffer() -> None:
    """Spec 06 invariant 5: the rolling buffer is cleared on switch.

    Old-branch percentiles would otherwise pollute the new branch's
    fresh stats and the dashboard p95 would not visibly drop.
    """
    from slowquery_demo.core.observability import on_branch_switch
    from slowquery_demo.main import create_app

    app = create_app()
    buffer = app.state.slowquery_buffer
    buffer.record("deadbeef", 1200.0)
    assert "deadbeef" in buffer.keys()  # noqa: SIM118 — RingBuffer.keys() returns a frozenset

    await on_branch_switch(app)

    assert buffer.keys() == frozenset()


@pytest.mark.asyncio
async def test_on_branch_switch_reattaches_hooks_to_new_engine() -> None:
    """F: hooks re-attach to the swapped engine so new samples flow.

    Simulate a swap by replacing ``app.state.engine`` with a fresh
    engine, then assert the patched ``attach`` is invoked against it.
    """
    from unittest.mock import MagicMock, patch

    from slowquery_demo.core.observability import on_branch_switch
    from slowquery_demo.main import create_app

    app = create_app()
    new_engine = MagicMock(name="new_engine")
    app.state.engine = new_engine

    with patch("slowquery_demo.core.observability._patched_attach") as mock_attach:
        await on_branch_switch(app)

    mock_attach.assert_called_once()
    called_engine = mock_attach.call_args.args[0]
    assert called_engine is new_engine


def test_reattach_slowquery_noop_when_not_installed() -> None:
    """``reattach_slowquery`` is a safe no-op if the pipeline is absent."""
    from fastapi import FastAPI

    from slowquery_demo.core.observability import reattach_slowquery

    app = FastAPI()
    assert reattach_slowquery(app) is False


# --- Drainer composed flow: OPT-1 recompute throttle + COST-1 retention prune ---
#
# The store primitives (``record_sample(recompute_stats=...)`` and
# ``prune_samples``) are unit-tested in isolation in test_01. These tests
# exercise the *decision* the drainer makes on the real per-item drain path
# (``_drain_one``) with an injected clock, closing the refinement-audit gap:
# a regression that reverted the throttle to "always recompute" (OPT-1) or
# stopped the periodic prune from ever firing (COST-1) would previously pass
# every test because ``_drain_one`` had zero coverage.


def _drain_store_mock():  # type: ignore[no-untyped-def]
    """A store whose write hooks are all recording AsyncMocks."""
    from unittest.mock import AsyncMock

    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.prune_samples = AsyncMock()
    return store


def _bridge_item(duration_ms: float, fp_id: str = "abc123"):  # type: ignore[no-untyped-def]
    """A ``_BridgeItem`` = (fp_id, canonical_sql, raw_statement, params, duration_ms)."""
    return (fp_id, "SELECT 1", "SELECT 1", (), duration_ms)


async def test_drain_one_recomputes_stats_on_first_sample_then_throttles() -> None:
    """OPT-1: first sample per fingerprint recomputes percentiles; a second
    sample inside ``stats_interval`` only bumps totals (recompute_stats=False)."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    # retention_s=0 isolates the throttle decision from the prune decision.
    state = _DrainState(threshold_ms=1_000_000.0, retention_s=0.0, stats_interval=2.0)

    await _drain_one(store, state, _bridge_item(50.0), now=100.0)
    assert store.record_sample.await_args.kwargs["recompute_stats"] is True
    assert state.last_stats["abc123"] == 100.0

    # 1s later — still inside the 2s window — must NOT recompute.
    await _drain_one(store, state, _bridge_item(50.0), now=101.0)
    assert store.record_sample.await_args.kwargs["recompute_stats"] is False
    assert state.last_stats["abc123"] == 100.0  # last-recompute stamp unchanged


async def test_drain_one_recomputes_again_after_interval_elapses() -> None:
    """OPT-1: once ``stats_interval`` has elapsed the recompute fires again and
    the per-fingerprint stamp advances."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(threshold_ms=1_000_000.0, retention_s=0.0, stats_interval=2.0)
    state.last_stats["abc123"] = 100.0

    await _drain_one(store, state, _bridge_item(50.0), now=103.0)  # 3s >= 2s window
    assert store.record_sample.await_args.kwargs["recompute_stats"] is True
    assert state.last_stats["abc123"] == 103.0


async def test_drain_one_always_recomputes_when_interval_nonpositive() -> None:
    """OPT-1: ``stats_interval <= 0`` disables the throttle (legacy behaviour)."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(threshold_ms=1_000_000.0, retention_s=0.0, stats_interval=0.0)

    await _drain_one(store, state, _bridge_item(50.0), now=100.0)
    assert store.record_sample.await_args.kwargs["recompute_stats"] is True


async def test_drain_one_anchors_prune_clock_and_skips_first_prune() -> None:
    """COST-1: the prune clock is anchored on the first item so a prune cannot
    fire on the very first observed query."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(
        threshold_ms=1_000_000.0, retention_s=86_400.0, stats_interval=0.0, prune_interval_s=300.0
    )
    assert state.last_prune is None

    await _drain_one(store, state, _bridge_item(50.0), now=500.0)
    assert state.last_prune == 500.0
    store.prune_samples.assert_not_awaited()


async def test_drain_one_does_not_prune_within_interval() -> None:
    """COST-1: no prune while less than ``prune_interval_s`` has elapsed."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(
        threshold_ms=1_000_000.0, retention_s=86_400.0, stats_interval=0.0, prune_interval_s=300.0
    )
    state.last_prune = 100.0

    await _drain_one(store, state, _bridge_item(50.0), now=200.0)  # 100s < 300s
    store.prune_samples.assert_not_awaited()
    assert state.last_prune == 100.0  # clock not advanced


async def test_drain_one_prunes_after_interval_elapses() -> None:
    """COST-1: once ``prune_interval_s`` has elapsed the drainer fires the
    retention DELETE with the configured retention, and re-anchors the clock."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(
        threshold_ms=1_000_000.0, retention_s=86_400.0, stats_interval=0.0, prune_interval_s=300.0
    )
    state.last_prune = 100.0

    await _drain_one(store, state, _bridge_item(50.0), now=500.0)  # 400s >= 300s
    store.prune_samples.assert_awaited_once_with(86_400.0)
    assert state.last_prune == 500.0


async def test_drain_one_never_prunes_when_retention_disabled() -> None:
    """COST-1: ``retention_s <= 0`` disables the periodic prune entirely, even
    long after ``prune_interval_s`` would otherwise have elapsed."""
    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    state = _DrainState(
        threshold_ms=1_000_000.0, retention_s=0.0, stats_interval=0.0, prune_interval_s=300.0
    )
    state.last_prune = 100.0

    await _drain_one(store, state, _bridge_item(50.0), now=1_000_000.0)
    store.prune_samples.assert_not_awaited()


def test_build_drain_state_wires_tunables_from_settings() -> None:
    """OPT-1/COST-1 wiring: the drainer's throttle/prune tunables come from the
    live Settings + app.state, so the decisions the tests above exercise are the
    ones running on the deployed drain path (not test-only defaults)."""
    from slowquery_demo.core.observability import _build_drain_state
    from slowquery_demo.main import create_app

    app = create_app()
    state = _build_drain_state(app)

    assert state.threshold_ms == app.state.slowquery_threshold_ms
    assert state.retention_s == app.state.settings.slowquery_sample_retention_s
    assert state.stats_interval == app.state.settings.slowquery_stats_recompute_interval_s


async def test_drain_one_prune_failure_is_swallowed_not_fatal() -> None:
    """COST-1 negative-space: a failing prune must not abort the drain loop —
    the fingerprint/sample writes still happened and the item completes."""
    from unittest.mock import AsyncMock

    from slowquery_demo.core.observability import _drain_one, _DrainState

    store = _drain_store_mock()
    store.prune_samples = AsyncMock(side_effect=RuntimeError("neon down"))
    state = _DrainState(
        threshold_ms=1_000_000.0, retention_s=86_400.0, stats_interval=0.0, prune_interval_s=300.0
    )
    state.last_prune = 100.0

    # Must not raise despite prune_samples blowing up.
    await _drain_one(store, state, _bridge_item(50.0), now=500.0)
    store.record_sample.assert_awaited_once()
