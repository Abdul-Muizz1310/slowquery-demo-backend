"""Integration tests for spec 15 (the demo on slowquery-detective 0.2).

Real Postgres (Testcontainers), the real app with its lifespan, the library's
own store in schema ``slowquery``. The library flushes before it reads, so a
read route right after a request sees that request.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _first_user(engine: Any) -> str:
    async with engine.connect() as conn:
        return str(await conn.scalar(text("SELECT id FROM users LIMIT 1")))


async def test_11_request_id_lists_exactly_that_requests_queries(
    seeded_app: Any, pg_engine_noop: Any
) -> None:
    """Spec 15 case 11 (the WP-8 acceptance shape)."""
    user_id = await _first_user(pg_engine_noop)
    rid = f"wp8-{uuid.uuid4().hex[:12]}"

    assert (
        seeded_app.get(f"/users/{user_id}/orders", headers={"X-Request-Id": rid}).status_code == 200
    )
    seeded_app.get(f"/users/{user_id}/orders")  # untagged: adds nothing to rid

    rows = seeded_app.get("/_slowquery/queries", params={"request_id": rid}).json()
    assert rows, "the tagged request's fingerprints must be listed"
    by_sql = {row["fingerprint"]: row for row in rows}
    orders = [sql for sql in by_sql if "from orders" in sql.lower()]
    assert orders, by_sql.keys()
    assert all(by_sql[sql]["call_count"] == 1 for sql in orders)
    assert all(row["p95_ms"] is None for row in rows)

    other = seeded_app.get("/_slowquery/queries", params={"request_id": "never-sent"}).json()
    assert other == []


async def test_15_health_probe_is_never_a_fingerprint(seeded_app: Any) -> None:
    """Spec 15 case 15."""
    for _ in range(3):
        assert seeded_app.get("/health").json()["db"] == "ok"
    rows = seeded_app.get("/_slowquery/queries", params={"limit": 500}).json()
    assert not [row for row in rows if row["fingerprint"].strip().lower().startswith("select ?")]
    assert not [row for row in rows if row["fingerprint"].strip().lower() == "select 1"]


async def test_16_library_schema_is_created_and_legacy_tables_untouched(
    pg_container: Any, monkeypatch: pytest.MonkeyPatch, pg_engine_noop: Any
) -> None:
    """Spec 15 case 16: a clean start in schema ``slowquery``; boot twice."""
    from fastapi.testclient import TestClient

    from tests.integration.conftest import _asyncpg_url, _migrate_fresh

    url = _asyncpg_url(pg_container.get_connection_url())
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATABASE_URL_FAST", url)
    _migrate_fresh(url)
    async with pg_engine_noop.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO query_fingerprints (id, fingerprint) VALUES ('0123456789abcdef', 'legacy')"
            )
        )

    from slowquery_demo.main import create_app

    for _ in range(2):  # a second boot over the same database succeeds
        with TestClient(create_app()) as client:
            assert client.get("/_slowquery/queries").status_code == 200

    async with pg_engine_noop.connect() as conn:
        tables = {
            row[0]
            for row in await conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'slowquery'"
                )
            )
        }
        legacy = await conn.scalar(text("SELECT fingerprint FROM public.query_fingerprints"))
    assert {
        "query_fingerprints",
        "request_stats",
        "query_samples",
        "explain_plans",
        "suggestions",
    } <= tables
    assert legacy == "legacy"


async def test_17_force_explain_asks_the_llm_once_when_rules_miss(
    seeded_app_llm: Any, pg_engine_noop: Any, respx_mock: Any
) -> None:
    """Spec 15 case 17 (spec 05 test 9 on the library store)."""
    from httpx import Response

    openrouter = respx_mock.route(host="openrouter.ai").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"diagnosis": "x", "suggestion": null, '
                            '"confidence": 0.1, "kind": "unknown"}'
                        }
                    }
                ]
            },
        )
    )
    user_id = await _first_user(pg_engine_noop)
    seeded_app_llm.get(f"/users/{user_id}/orders")
    rows = seeded_app_llm.get("/_slowquery/queries").json()
    fingerprint_id = next(r["id"] for r in rows if "from orders" in r["fingerprint"].lower())

    calls_before = openrouter.call_count
    resp = seeded_app_llm.post(
        f"/_slowquery/queries/{fingerprint_id}/force-explain",
        headers={"X-Admin-Token": "test-admin-token"},
    )
    assert resp.status_code == 200, resp.text
    assert openrouter.call_count == calls_before + 1
    detail = seeded_app_llm.get(f"/_slowquery/queries/{fingerprint_id}").json()
    assert detail["explain_plan"]["plan_json"]["Plan"]["Node Type"] == "Result"


async def test_shutdown_closes_the_library_store(test_client_lifespan: Any) -> None:
    """Spec 05 test 10, now the library's store."""
    with test_client_lifespan as client:
        client.get("/health")
    assert test_client_lifespan.store_close_calls == 1  # type: ignore[attr-defined]


async def test_13_slow_branch_plan_shows_the_scan_the_rules_key_on(
    seeded_app_slow: Any, pg_engine_noop: Any
) -> None:
    """Spec 15 case 13: the library EXPLAINs the slow ``ORDER BY created_at`` on the
    slow branch (real parameters, no ANALYZE) and stores a plan with its Seq Scan.

    Whether a rule then fires depends on the seed size (DEVIATIONS §2: at the
    test's 1,000 orders the sort is too cheap for ``sort_without_index``), so
    the stored plan is the assertion; any suggestion present must be an index.
    """
    import json

    deadline = time.monotonic() + 30
    detail: dict[str, Any] = {}
    while time.monotonic() < deadline:
        seeded_app_slow.get("/orders", params={"limit": 20})
        time.sleep(1.0)
        rows = seeded_app_slow.get("/_slowquery/queries", params={"limit": 500}).json()
        ids = [
            r["id"]
            for r in rows
            if "from orders" in r["fingerprint"].lower()
            and "order by" in r["fingerprint"].lower()
            and "user_id =" not in r["fingerprint"].lower()
        ]
        if ids:
            detail = seeded_app_slow.get(f"/_slowquery/queries/{ids[0]}").json()
            if detail.get("explain_plan"):
                break
    assert detail.get("explain_plan"), detail
    assert "Seq Scan" in json.dumps(detail["explain_plan"]["plan_json"])
    assert all(s["kind"] == "index" for s in detail["suggestions"])
