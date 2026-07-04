"""S4: unit-lane tests for spec 08 (dashboard read API).

Tests exercise the expanded /_slowquery/queries and queries/{id}
endpoints. The ``test_client`` fixture provides an empty mock session,
so all the "happy with data" cases are integration-lane. Here we cover:

- Happy (with empty result set, verifying response shape)
- Failure (404 paths)
- Security (script injection in fingerprints)
"""

from __future__ import annotations


def test_list_queries_returns_list(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 08 test 6: no fingerprints → []."""
    resp = test_client.get("/_slowquery/queries")
    assert resp.status_code == 200
    assert resp.json() == []


def test_get_query_detail_unknown_id_404(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 08 test 10: unknown fingerprint id → 404."""
    resp = test_client.get("/_slowquery/queries/abcdef1234567890")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "not found"


def test_get_query_detail_invalid_id_shape_404(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 08 test 11: invalid hex id shape → 404 before DB call."""
    resp = test_client.get("/_slowquery/queries/NOT-HEX-ID-HERE!")
    assert resp.status_code == 404


def test_openapi_exposes_two_dashboard_endpoints(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 08 acceptance: both endpoints registered in the schema."""
    schema = test_client.get("/openapi.json").json()
    paths = set(schema["paths"].keys())
    assert "/_slowquery/queries" in paths
    assert "/_slowquery/queries/{fingerprint_id}" in paths


def test_fingerprint_id_regex_accepts_valid_hex_ids() -> None:
    """Spec 08 test 11 (negative-space): the id validator accepts 1..16 lowercase hex."""
    from slowquery_demo.api.routers.dashboard import _FINGERPRINT_ID_RE

    assert _FINGERPRINT_ID_RE.match("abcdef1234567890") is not None
    assert _FINGERPRINT_ID_RE.match("a") is not None
    assert _FINGERPRINT_ID_RE.match("0123456789abcdef") is not None


def test_fingerprint_id_regex_rejects_out_of_alphabet_or_length() -> None:
    """Spec 08 test 11 (negative-space): empty, uppercase, punctuation, and >16
    chars are all rejected before any DB lookup — the id can't smuggle a payload."""
    from slowquery_demo.api.routers.dashboard import _FINGERPRINT_ID_RE

    assert _FINGERPRINT_ID_RE.match("") is None
    assert _FINGERPRINT_ID_RE.match("ABCDEF") is None  # uppercase
    assert _FINGERPRINT_ID_RE.match("xyz!") is None  # punctuation / non-hex
    assert _FINGERPRINT_ID_RE.match("abcdef12345678901") is None  # 17 chars


def test_extract_rule_returns_none_regardless_of_source() -> None:
    """Spec 08: ``_extract_rule`` is a documented no-op today (the rule name is
    not parsed out of the rationale); it must return ``None`` for every source
    rather than raise, so the detail endpoint stays total."""
    from slowquery_demo.api.routers.dashboard import _extract_rule

    assert _extract_rule("Seq Scan on orders", "rules") is None
    assert _extract_rule("some rationale", "llm") is None
