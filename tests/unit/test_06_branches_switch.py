"""S4 unit tests for spec 06 (POST /branches/switch).

Tests here exercise request/response validation, schema Literal
enforcement, and branch-state file persistence. Fixture-heavy tests
(concurrent switches, dead-pool rollback, non-demo-mode 403, real
engine swap) live in tests/integration/test_06_branches_switch.py
and are enabled in S5 when the integration conftest lands.
"""

from __future__ import annotations

import pytest


def test_invalid_target_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 11."""
    resp = test_client.post("/branches/switch", json={"target": "banana"})
    assert resp.status_code == 422


def test_missing_target_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 12."""
    resp = test_client.post("/branches/switch", json={})
    assert resp.status_code == 422


def test_malformed_branch_state_file_defaults_to_slow(  # type: ignore[no-untyped-def]
    tmp_path, monkeypatch
) -> None:
    """Spec 06 test 14."""
    state = tmp_path / ".branch_state"
    state.write_text("zonk")
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state))

    from slowquery_demo.core.branch_state import load_branch

    assert load_branch() == "slow"


def test_pydantic_literal_validation() -> None:
    """Spec 06 test 15."""
    from pydantic import ValidationError

    from slowquery_demo.schemas.branches import SwitchBranchRequest

    with pytest.raises(ValidationError):
        SwitchBranchRequest(target="banana")  # type: ignore[arg-type]


def test_no_ddl_payload_accepted(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 18: OpenAPI schema does not expose a sql field."""
    schema = test_client.get("/openapi.json").json()
    body_schema = schema["paths"]["/branches/switch"]["post"]["requestBody"]
    assert "sql" not in str(body_schema)


def test_switch_returns_503_when_switcher_not_wired() -> None:
    """Spec 06 negative-space: if the branch switcher was never wired onto
    ``app.state`` the endpoint fails closed with 503, never a 500/AttributeError."""
    from fastapi.testclient import TestClient

    from slowquery_demo.main import create_app

    app = create_app()
    # Simulate a startup where the switcher was not installed.
    if hasattr(app.state, "branch_switcher"):
        del app.state.branch_switcher

    with TestClient(app) as client:
        resp = client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 503


def test_switch_to_fast_returns_200(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 1."""
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "fast"
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] > 0
    assert "switched_at" in body


def test_second_switch_to_same_target_returns_409(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 2."""
    test_client.post("/branches/switch", json={"target": "fast"})
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 409


def test_response_shape(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 4."""
    import datetime as dt

    resp = test_client.post("/branches/switch", json={"target": "fast"})
    body = resp.json()
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] > 0
    dt.datetime.fromisoformat(body["switched_at"])


@pytest.mark.asyncio
async def test_buffer_cleared_after_successful_switch() -> None:
    """Spec 06 test 5: the buffer on app.state is cleared after a switch.

    Wires the real ``BranchSwitcher`` from ``create_app`` with a fake
    engine_builder (so no Neon is dialed) and the real ``post_switch``
    hook. A sample is recorded into the buffer before the switch and the
    buffer must be empty afterward.
    """
    from unittest.mock import MagicMock, patch

    from slowquery_demo.main import create_app

    app = create_app()
    buffer = app.state.slowquery_buffer
    buffer.record("c168fc78", 1200.0)
    assert "c168fc78" in buffer.keys()  # noqa: SIM118 — RingBuffer.keys() returns a frozenset

    # Replace the engine_builder with a fake that swaps in a new engine,
    # mirroring what the production closure does, but without Neon.
    switcher = app.state.branch_switcher

    async def _fake_builder(url: str) -> tuple[object, object]:
        app.state.engine = MagicMock(name="rebuilt_engine")
        app.state.db_sessionmaker = MagicMock(name="rebuilt_factory")
        return app.state.engine, app.state.db_sessionmaker

    switcher._engine_builder = _fake_builder  # type: ignore[attr-defined]

    # Re-attach is exercised separately; stub it so this test isolates
    # the buffer-clear invariant.
    with patch(
        "slowquery_demo.core.observability.reattach_slowquery",
        new=MagicMock(return_value=True),
    ):
        await switcher.switch("fast")

    assert buffer.keys() == frozenset()
    assert switcher.active == "fast"


# --- GET /branches/current (spec 06 tests 20-23) ------------------------


def test_current_branch_defaults_to_slow(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 20."""
    resp = test_client.get("/branches/current")
    assert resp.status_code == 200
    assert resp.json() == {"active": "slow"}


def test_current_branch_reflects_a_completed_switch(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 21."""
    assert test_client.post("/branches/switch", json={"target": "fast"}).status_code == 200

    resp = test_client.get("/branches/current")
    assert resp.status_code == 200
    assert resp.json() == {"active": "fast"}


def test_current_branch_is_not_throttled_by_the_mutation_cooldown(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Spec 06 test 22: the read-only companion is pollable.

    With a positive ``DEMO_MUTATION_COOLDOWN_S`` a second *mutation* inside
    the window is 429'd. ``GET /branches/current`` must be exempt, so a
    dashboard can poll it once a second without being rate-limited.
    """
    from fastapi.testclient import TestClient

    monkeypatch.setenv("DEMO_MUTATION_COOLDOWN_S", "30")

    from slowquery_demo.main import create_app

    app = create_app()
    assert app.state.settings.demo_mutation_cooldown_s == 30.0

    with TestClient(app) as client:
        for _ in range(3):
            resp = client.get("/branches/current")
            assert resp.status_code == 200, resp.text
            assert resp.json()["active"] == "slow"


def test_current_branch_returns_503_when_switcher_not_wired() -> None:
    """Spec 06 test 23: fails closed, never a 500/AttributeError."""
    from fastapi.testclient import TestClient

    from slowquery_demo.main import create_app

    app = create_app()
    if hasattr(app.state, "branch_switcher"):
        del app.state.branch_switcher

    with TestClient(app) as client:
        resp = client.get("/branches/current")
    assert resp.status_code == 503


def test_engine_builder_failure_leaves_buffer_intact() -> None:
    """A failed switch must not clear the buffer (old branch stays live)."""
    import asyncio
    from unittest.mock import AsyncMock

    from slowquery_demo.main import create_app

    app = create_app()
    buffer = app.state.slowquery_buffer
    buffer.record("c168fc78", 1200.0)

    switcher = app.state.branch_switcher
    failing = AsyncMock(side_effect=ConnectionError("neon down"))
    switcher._engine_builder = failing  # type: ignore[attr-defined]

    with pytest.raises(ConnectionError):
        asyncio.run(switcher.switch("fast"))

    assert "c168fc78" in buffer.keys()  # noqa: SIM118 — RingBuffer.keys() returns a frozenset
    assert switcher.active == "slow"
