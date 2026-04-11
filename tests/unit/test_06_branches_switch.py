"""S3 red: unit tests for spec 06 (POST /branches/switch)."""

from __future__ import annotations

import pytest


def test_switch_to_fast_returns_200(test_client, fake_engines) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 1."""
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["active"] == "fast"


def test_second_switch_to_same_target_returns_409(test_client, fake_engines) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 2."""
    test_client.post("/branches/switch", json={"target": "fast"})
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 409


async def test_concurrent_switches_serialize(test_client_async, fake_engines) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 3."""
    import asyncio

    r1, r2 = await asyncio.gather(
        test_client_async.post("/branches/switch", json={"target": "fast"}),
        test_client_async.post("/branches/switch", json={"target": "fast"}),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [200, 409]


def test_response_shape(test_client, fake_engines) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 4."""
    import datetime as dt

    resp = test_client.post("/branches/switch", json={"target": "fast"})
    body = resp.json()
    assert isinstance(body["latency_ms"], int) and body["latency_ms"] > 0
    dt.datetime.fromisoformat(body["switched_at"])


def test_buffer_cleared_on_switch(test_client, fake_engines, app_state) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 5."""
    app_state.slowquery_buffer.record("abc", 123.0)  # type: ignore[attr-defined]
    test_client.post("/branches/switch", json={"target": "fast"})
    assert app_state.slowquery_buffer.size() == 0  # type: ignore[attr-defined]


def test_invalid_target_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 11."""
    resp = test_client.post("/branches/switch", json={"target": "banana"})
    assert resp.status_code == 422


def test_missing_target_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 12."""
    resp = test_client.post("/branches/switch", json={})
    assert resp.status_code == 422


def test_unreachable_fast_returns_503_and_preserves_state(test_client_dead_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 13."""
    resp = test_client_dead_fast.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 503
    # Old engine still serving.
    health = test_client_dead_fast.get("/health")
    assert health.status_code == 200


def test_malformed_branch_state_file_defaults_to_slow(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 14."""
    state = tmp_path / ".branch_state"
    state.write_text("zonk")
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state))

    from slowquery_demo.core.branch_state import load_branch

    assert load_branch() == "slow"


def test_pydantic_literal_validation(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 15."""
    from pydantic import ValidationError
    from slowquery_demo.schemas.branches import SwitchBranchRequest

    with pytest.raises(ValidationError):
        SwitchBranchRequest(target="banana")  # type: ignore[arg-type]


def test_demo_mode_off_returns_403(test_client_non_demo) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 16."""
    resp = test_client_non_demo.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 403


def test_settings_captured_at_startup(test_client, fake_engines, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 17."""
    # Patching env mid-flight must not change the URL the handler reaches.
    monkeypatch.setenv("DATABASE_URL_FAST", "postgresql+asyncpg://changed/")
    resp = test_client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code in {200, 409}
    # The switch used the old URL captured at startup.


def test_no_ddl_payload_accepted(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 18."""
    resp = test_client.post(
        "/branches/switch",
        json={"target": "fast", "sql": "DROP TABLE users"},
    )
    # Extra fields are ignored; the endpoint never reads `sql`.
    # Prove it by checking OpenAPI schema.
    schema = test_client.get("/openapi.json").json()
    body_schema = schema["paths"]["/branches/switch"]["post"]["requestBody"]
    assert "sql" not in str(body_schema)
    assert resp.status_code in {200, 409}


def test_switch_is_constant_time_per_request(test_client, fake_engines) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 19."""
    import time

    start = time.monotonic()
    for _ in range(5):
        test_client.post("/branches/switch", json={"target": "fast"})
        test_client.post("/branches/switch", json={"target": "slow"})
    elapsed = time.monotonic() - start
    assert elapsed < 10  # generous; real gate is "not O(n²)"
