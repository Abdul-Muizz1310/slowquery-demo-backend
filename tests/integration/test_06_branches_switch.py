"""S3 red: integration tests for spec 06 (branch switching)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_switch_routes_subsequent_queries_to_fast_branch(
    dual_pg_app, pg_engine_slow, pg_engine_fast
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 6."""
    resp = dual_pg_app.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200
    # A subsequent /users call should reach the fast container.
    r = dual_pg_app.get("/users?limit=1")
    assert r.status_code == 200


async def test_row_identity_preserved_across_switch(
    dual_pg_app, pg_engine_slow, pg_engine_fast
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 7."""
    before = {r["email"] for r in dual_pg_app.get("/users?limit=100").json()["items"]}
    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    after = {r["email"] for r in dual_pg_app.get("/users?limit=100").json()["items"]}
    assert before == after


async def test_explain_transitions_from_seq_to_index(
    dual_pg_app, pg_engine_slow, pg_engine_fast
) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 8."""
    import json

    from sqlalchemy import text

    async with pg_engine_slow.connect() as conn:
        sample_user = await conn.scalar(text("SELECT id FROM users LIMIT 1"))
        slow_plan = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    assert json.loads(str(slow_plan))[0]["Plan"]["Node Type"] == "Seq Scan"

    dual_pg_app.post("/branches/switch", json={"target": "fast"})

    async with pg_engine_fast.connect() as conn:
        fast_plan = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    assert "Index" in json.loads(str(fast_plan))[0]["Plan"]["Node Type"]


async def test_fingerprints_recorded_after_switch(dual_pg_app, pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 9."""
    import asyncio

    from sqlalchemy import text

    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    dual_pg_app.get("/users?limit=5")
    await asyncio.sleep(0.5)

    async with pg_engine_fast.connect() as conn:
        count = await conn.scalar(text("SELECT COUNT(*) FROM query_fingerprints"))
    assert count >= 1


async def test_branch_state_persists_across_restart(dual_pg_app, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Spec 06 test 10."""
    dual_pg_app.post("/branches/switch", json={"target": "fast"})
    # Simulate restart by recreating the app instance.
    from slowquery_demo.main import create_app

    app2 = create_app()
    assert app2.state.branch_current == "fast"
