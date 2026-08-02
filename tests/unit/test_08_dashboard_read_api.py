"""S4: unit-lane tests for spec 08 (dashboard read API).

Tests exercise the expanded /_slowquery/queries and queries/{id} endpoints.

The ``test_client`` fixture's session mock returns empty result sets, which used
to push every "happy with data" case (spec 08 tests 1-5, 7-9, 12) into the
integration lane — leaving the detail handler, the DTO mapping and the
suggestion grouping with no unit coverage at all. Those cases are covered here
by substituting the *repository* functions with ones returning real ORM
instances: the router layer is what is under test, and the repository has its
own tests plus the Testcontainers lane.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest


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


# --- Happy paths with data (spec 08 tests 1-5, 7-9, 12) -----------------
#
# The repository layer is substituted so the *router's* DTO mapping is what is
# exercised: `FingerprintResponse` construction, the per-fingerprint suggestion
# grouping, `canonical_sql` derivation, and the optional plan / samples blocks.

_NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)


def _fingerprint(fp_id: str, *, total_ms: int, sql: str | None = None) -> Any:
    from slowquery_demo.models.slowquery_store import QueryFingerprint

    return QueryFingerprint(
        id=fp_id,
        fingerprint=sql or f"select * from orders where id = ? -- {fp_id}",
        first_seen=_NOW,
        last_seen=_NOW,
        call_count=7,
        total_ms=total_ms,
        p50_ms=1.5,
        p95_ms=2.5,
        p99_ms=3.5,
        max_ms=4.5,
    )


def _suggestion(row_id: int, fp_id: str, *, source: str = "rules") -> Any:
    from slowquery_demo.models.slowquery_store import Suggestion

    return Suggestion(
        id=row_id,
        fingerprint_id=fp_id,
        kind="index",
        sql="CREATE INDEX IF NOT EXISTS ix_orders_created_at ON orders(created_at);",
        source=source,
        rationale="Sort node on ORDER BY created_at with significant cost.",
        applied_at=None,
    )


@pytest.fixture
def stub_repo(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Replace the dashboard router's repository calls with in-memory returns.

    Yields the mutable table of return values so each test declares only the
    rows it cares about.
    """
    import slowquery_demo.api.routers.dashboard as dash

    tables: dict[str, Any] = {
        "fingerprints": [],
        "all_suggestions": {},
        "fingerprint": None,
        "suggestions": [],
        "plan": None,
        "samples": [],
    }

    async def _list_fingerprints(_session: object) -> list[Any]:
        return list(tables["fingerprints"])

    async def _list_all_suggestions(_session: object) -> dict[str, list[Any]]:
        return dict(tables["all_suggestions"])

    async def _get_fingerprint_by_id(_session: object, fingerprint_id: str) -> Any:
        fp = tables["fingerprint"]
        return fp if fp is not None and fp.id == fingerprint_id else None

    async def _list_suggestions_for_fingerprint(_s: object, _fp: str) -> list[Any]:
        return list(tables["suggestions"])

    async def _get_explain_plan(_s: object, _fp: str) -> Any:
        return tables["plan"]

    async def _list_recent_samples(_s: object, _fp: str, **_kw: object) -> list[Any]:
        return list(tables["samples"])

    monkeypatch.setattr(dash.repo, "list_fingerprints", _list_fingerprints)
    monkeypatch.setattr(dash.repo, "list_all_suggestions", _list_all_suggestions)
    monkeypatch.setattr(dash.repo, "get_fingerprint_by_id", _get_fingerprint_by_id)
    monkeypatch.setattr(
        dash.repo, "list_suggestions_for_fingerprint", _list_suggestions_for_fingerprint
    )
    monkeypatch.setattr(dash.repo, "get_explain_plan", _get_explain_plan)
    monkeypatch.setattr(dash.repo, "list_recent_samples", _list_recent_samples)
    yield tables


def test_list_queries_returns_every_fingerprint_with_its_own_suggestions(  # type: ignore[no-untyped-def]
    test_client,
    stub_repo,
) -> None:
    """Spec 08 tests 1-2: the repository's order is preserved and each item
    carries *its own* suggestions (grouped by id, not cross-contaminated)."""
    stub_repo["fingerprints"] = [
        _fingerprint("aaa1", total_ms=900),
        _fingerprint("bbb2", total_ms=500),
        _fingerprint("ccc3", total_ms=100),
    ]
    stub_repo["all_suggestions"] = {
        "aaa1": [_suggestion(1, "aaa1"), _suggestion(2, "aaa1")],
        "ccc3": [_suggestion(3, "ccc3", source="llm")],
    }

    body = test_client.get("/_slowquery/queries").json()

    assert [item["id"] for item in body] == ["aaa1", "bbb2", "ccc3"]
    assert [len(item["suggestions"]) for item in body] == [2, 0, 1]
    assert body[0]["call_count"] == 7
    assert body[0]["p95_ms"] == 2.5
    # ``rule`` is the documented no-op derivation; ``source`` is the real signal.
    assert body[2]["suggestions"][0]["source"] == "llm"
    assert body[2]["suggestions"][0]["rule"] is None


def test_detail_returns_plan_samples_and_canonical_sql(test_client, stub_repo) -> None:  # type: ignore[no-untyped-def]
    """Spec 08 tests 3-5: canonical_sql is the fingerprint text, and the plan
    plus the recent samples come back in the detail envelope."""
    from slowquery_demo.models.slowquery_store import ExplainPlan, QuerySample

    fp = _fingerprint("abc123", total_ms=1234)
    stub_repo["fingerprint"] = fp
    stub_repo["suggestions"] = [_suggestion(9, "abc123")]
    stub_repo["plan"] = ExplainPlan(
        fingerprint_id="abc123",
        plan_json={"Plan": {"Node Type": "Seq Scan", "Total Cost": 42.5}},
        plan_text='{"Plan": {"Node Type": "Seq Scan"}}',
        cost=42.5,
        captured_at=_NOW,
    )
    stub_repo["samples"] = [
        QuerySample(
            id=n,
            fingerprint_id="abc123",
            params=None,
            duration_ms=float(n) * 10,
            rows=n,
            sampled_at=_NOW,
        )
        for n in (3, 2, 1)
    ]

    resp = test_client.get("/_slowquery/queries/abc123")

    assert resp.status_code == 200
    body = resp.json()
    assert body["canonical_sql"] == fp.fingerprint
    assert body["fingerprint"]["id"] == "abc123"
    assert body["fingerprint"]["suggestions"][0]["id"] == 9
    assert body["explain_plan"]["cost"] == 42.5
    assert body["explain_plan"]["plan_json"]["Plan"]["Node Type"] == "Seq Scan"
    assert [s["duration_ms"] for s in body["recent_samples"]] == [30.0, 20.0, 10.0]
    assert len(body["suggestions"]) == 1


def test_detail_with_nothing_recorded_yet_returns_empty_blocks(  # type: ignore[no-untyped-def]
    test_client,
    stub_repo,
) -> None:
    """Spec 08 tests 7-9: a fingerprint with no plan / suggestions / samples
    still returns 200 with ``null`` and ``[]`` — never a 500."""
    stub_repo["fingerprint"] = _fingerprint("dead", total_ms=1)

    body = test_client.get("/_slowquery/queries/dead").json()

    assert body["explain_plan"] is None
    assert body["suggestions"] == []
    assert body["recent_samples"] == []
    assert body["fingerprint"]["suggestions"] == []


def test_script_payload_in_fingerprint_is_returned_json_encoded(  # type: ignore[no-untyped-def]
    test_client,
    stub_repo,
) -> None:
    """Spec 08 test 12: SQL text is data, not markup.

    The contract is explicit: the fingerprint is returned **as-is**, JSON-encoded,
    and the frontend owns escaping. So the assertion is round-trip fidelity (no
    server-side stripping or HTML-escaping that would corrupt the SQL a reviewer
    reads) *plus* an ``application/json`` content type, which is what stops a
    browser from ever executing the payload.
    """
    hostile = "select 1 -- <script>alert('xss')</script>"
    stub_repo["fingerprints"] = [_fingerprint("beef", total_ms=1, sql=hostile)]

    resp = test_client.get("/_slowquery/queries")

    assert resp.json()[0]["fingerprint"] == hostile
    assert resp.headers["content-type"].startswith("application/json")
    assert "text/html" not in resp.headers["content-type"]
