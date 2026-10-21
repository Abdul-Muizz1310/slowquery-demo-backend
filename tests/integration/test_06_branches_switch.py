"""S3 red: integration tests for spec 06 (branch switching)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_switch_routes_subsequent_queries_to_fast_branch(
    dual_pg_app, pg_engine_fast_noop
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 6 / spec 15 case 14: after the switch, the app reads the fast database.

    A product that exists only on the fast container is invisible before the
    switch and listed after it, through the same observed engine.
    """
    from sqlalchemy import text

    async with pg_engine_fast_noop.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO products (id, sku, name, price_cents) "
                "VALUES (gen_random_uuid(), 'only-on-fast', 'marker', 100)"
            )
        )

    def skus() -> set[str]:
        body = dual_pg_app.get("/products?limit=100").json()
        return {row["sku"] for row in body["items"]}

    app = dual_pg_app.app  # type: ignore[attr-defined]
    engine = app.state.engine
    assert "only-on-fast" not in skus()

    resp = dual_pg_app.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200
    assert "only-on-fast" in skus()
    # The same engine object, still the one slowquery-detective observes and explains on.
    assert app.state.engine is engine
    assert app.state.slowquery_worker.engine is engine


async def test_row_identity_preserved_across_switch(
    dual_pg_app, pg_engine_noop, pg_engine_fast_noop
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 7."""
    before = {r["email"] for r in dual_pg_app.get("/users?limit=100").json()["items"]}
    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    after = {r["email"] for r in dual_pg_app.get("/users?limit=100").json()["items"]}
    assert before == after


async def test_explain_transitions_from_seq_to_index(
    dual_pg_app, pg_engine_noop, pg_engine_fast_noop
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 8."""
    import json

    from sqlalchemy import text

    async with pg_engine_noop.connect() as conn:
        sample_user = await conn.scalar(text("SELECT id FROM users LIMIT 1"))
        slow_plan = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    slow_plan_data = json.loads(slow_plan) if isinstance(slow_plan, str) else slow_plan
    assert slow_plan_data[0]["Plan"]["Node Type"] == "Seq Scan"

    dual_pg_app.post("/branches/switch", json={"target": "fast"})

    async with pg_engine_fast_noop.connect() as conn:
        fast_plan = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    fast_plan_data = json.loads(fast_plan) if isinstance(fast_plan, str) else fast_plan
    assert fast_plan_data[0]["Plan"]["Node Type"] != "Seq Scan"


async def test_fingerprints_recorded_after_switch(dual_pg_app) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 9 / spec 15 case 14: the library keeps recording after a switch."""
    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    rid = "after-switch-1"
    for _ in range(5):
        dual_pg_app.get("/users?limit=5", headers={"X-Request-Id": rid})

    rows = dual_pg_app.get("/_slowquery/queries", params={"request_id": rid}).json()
    users = [r for r in rows if "from users" in r["fingerprint"].lower()]
    assert users and users[0]["call_count"] == 5


async def test_branch_state_persists_across_restart(dual_pg_app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 10."""
    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    # Simulate restart by recreating the app instance.
    from slowquery_demo.main import create_app

    app2 = create_app()
    assert app2.state.branch_current == "fast"
