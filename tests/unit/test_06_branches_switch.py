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
