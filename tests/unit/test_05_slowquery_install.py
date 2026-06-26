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
