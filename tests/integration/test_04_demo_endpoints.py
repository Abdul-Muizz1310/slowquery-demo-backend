"""Integration tests for spec 04 (demo endpoints + seeded data).

Includes the data-dependent API tests that were originally staged in
``tests/unit/test_04_demo_endpoints.py`` in S3 (before we had a
Testcontainers conftest). They were moved here in S4 when the unit
lane was rebuilt around a mock-session test_client fixture.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# --- data-dependent API tests (moved from unit lane in S4) ----------


def test_list_users_returns_limited_rows_and_cursor(seeded_test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 1."""
    resp = seeded_test_client.get("/users?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 10
    assert body["next_cursor"] is not None


def test_list_user_orders_order_desc(seeded_test_client, sample_user_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 3."""
    resp = seeded_test_client.get(f"/users/{sample_user_id}/orders")
    assert resp.status_code == 200
    items = resp.json()["items"]
    created = [row["created_at"] for row in items]
    assert created == sorted(created, reverse=True)


def test_list_recent_orders(seeded_test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 4."""
    resp = seeded_test_client.get("/orders?limit=5")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 5


def test_get_order_with_items(seeded_test_client, sample_order_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 5."""
    resp = seeded_test_client.get(f"/orders/{sample_order_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_list_items_by_product(seeded_test_client, sample_product_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 6."""
    resp = seeded_test_client.get(f"/order_items?product_id={sample_product_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(row["product_id"] == str(sample_product_id) for row in items)


def test_cursor_roundtrip(seeded_test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 7."""
    first = seeded_test_client.get("/users?limit=10").json()
    second = seeded_test_client.get(f"/users?limit=10&cursor={first['next_cursor']}").json()
    first_ids = {row["id"] for row in first["items"]}
    second_ids = {row["id"] for row in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_limit_clamped_to_max_page_size(seeded_test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 13."""
    resp = seeded_test_client.get("/users?limit=10000")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 100


def test_dead_pool_returns_503(test_client_dead_pool) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 14."""
    resp = test_client_dead_pool.get("/users?limit=10")
    assert resp.status_code == 503


# --- middleware interaction tests -----------------------------------


async def test_fingerprint_recorded_for_user_orders(seeded_app, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 8."""
    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine)
    seeded_app.get(f"/users/{sample_user_id}/orders")

    async with pg_engine.connect() as conn:
        count = await conn.scalar(text("SELECT COUNT(*) FROM query_fingerprints"))
    assert count >= 1


async def test_rule_fires_on_slow_branch(seeded_app_slow, pg_engine_slow) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 9."""
    import asyncio

    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine_slow)
    seeded_app_slow.get(f"/users/{sample_user_id}/orders")
    await asyncio.sleep(5)

    async with pg_engine_slow.connect() as conn:
        suggestions = await conn.scalar(text("SELECT COUNT(*) FROM suggestions"))
    assert suggestions >= 1


async def test_rule_does_not_fire_on_fast_branch(seeded_app_fast, pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 10."""
    import asyncio

    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine_fast)
    seeded_app_fast.get(f"/users/{sample_user_id}/orders")
    await asyncio.sleep(5)

    async with pg_engine_fast.connect() as conn:
        suggestions = await conn.scalar(text("SELECT COUNT(*) FROM suggestions"))
    assert suggestions == 0


async def _first_user(engine) -> str:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    async with engine.connect() as conn:
        return str(await conn.scalar(text("SELECT id FROM users LIMIT 1")))
