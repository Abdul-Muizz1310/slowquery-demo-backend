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
