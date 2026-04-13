"""Additional unit tests for dashboard.py coverage.

Tests the _extract_rule helper and the fingerprint_id regex validation.
The actual API endpoint tests use the test_client fixture from conftest.
"""

from __future__ import annotations


def test_extract_rule_returns_none_for_non_rules_source() -> None:
    from slowquery_demo.api.routers.dashboard import _extract_rule

    assert _extract_rule("some rationale", "llm") is None


def test_extract_rule_returns_none_for_rules_source() -> None:
    """Even for rules source, _extract_rule currently returns None."""
    from slowquery_demo.api.routers.dashboard import _extract_rule

    assert _extract_rule("Seq Scan on orders", "rules") is None


def test_fingerprint_id_regex_accepts_valid() -> None:
    from slowquery_demo.api.routers.dashboard import _FINGERPRINT_ID_RE

    assert _FINGERPRINT_ID_RE.match("abcdef1234567890") is not None
    assert _FINGERPRINT_ID_RE.match("a") is not None
    assert _FINGERPRINT_ID_RE.match("0123456789abcdef") is not None


def test_fingerprint_id_regex_rejects_invalid() -> None:
    from slowquery_demo.api.routers.dashboard import _FINGERPRINT_ID_RE

    assert _FINGERPRINT_ID_RE.match("") is None
    assert _FINGERPRINT_ID_RE.match("ABCDEF") is None  # uppercase
    assert _FINGERPRINT_ID_RE.match("xyz!") is None
    assert _FINGERPRINT_ID_RE.match("abcdef12345678901") is None  # 17 chars


def test_list_queries_empty_via_client(test_client) -> None:  # type: ignore[no-untyped-def]
    """Verify the list endpoint returns [] with mock session."""
    resp = test_client.get("/_slowquery/queries")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_query_detail_bad_id_via_client(test_client) -> None:  # type: ignore[no-untyped-def]
    """Invalid fingerprint_id format returns 404 before DB call."""
    resp = test_client.get("/_slowquery/queries/NOT-VALID!")
    assert resp.status_code == 404


def test_get_query_detail_unknown_id_via_client(test_client) -> None:  # type: ignore[no-untyped-def]
    """Unknown but valid fingerprint_id returns 404."""
    resp = test_client.get("/_slowquery/queries/abcdef1234567890")
    assert resp.status_code == 404
