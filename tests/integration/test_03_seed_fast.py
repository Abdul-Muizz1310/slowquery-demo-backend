"""S3 red: integration tests for spec 03 (seed_fast.py)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_row_identity_between_slow_and_fast(pg_engine_slow, pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 4."""
    from sqlalchemy import text

    async with pg_engine_slow.connect() as conn:
        slow_users = {
            (r[0], r[1])
            for r in (await conn.execute(text("SELECT email, full_name FROM users"))).all()
        }
    async with pg_engine_fast.connect() as conn:
        fast_users = {
            (r[0], r[1])
            for r in (await conn.execute(text("SELECT email, full_name FROM users"))).all()
        }
    assert slow_users == fast_users


async def test_fast_branch_has_three_indexes(pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 5."""
    from sqlalchemy import text

    async with pg_engine_fast.connect() as conn:
        names = {r[0] for r in (await conn.execute(text("SELECT indexname FROM pg_indexes"))).all()}
    assert "ix_orders_user_id" in names
    assert "ix_order_items_order_id" in names
    assert "ix_order_items_product_id" in names


async def test_slow_branch_still_missing_indexes(pg_engine_slow) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 6."""
    from sqlalchemy import text

    async with pg_engine_slow.connect() as conn:
        names = {r[0] for r in (await conn.execute(text("SELECT indexname FROM pg_indexes"))).all()}
    assert "ix_orders_user_id" not in names
    assert "ix_order_items_order_id" not in names
    assert "ix_order_items_product_id" not in names


async def test_explain_uses_index_scan_on_fast(pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 7."""
    import json

    from sqlalchemy import text

    async with pg_engine_fast.connect() as conn:
        sample_user = await conn.scalar(text("SELECT id FROM users LIMIT 1"))
        raw = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    plan = json.loads(raw) if isinstance(raw, str) else raw
    node_type = plan[0]["Plan"]["Node Type"]
    assert "Index" in node_type


async def test_explain_uses_seq_scan_on_slow(pg_engine_slow) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 8."""
    import json

    from sqlalchemy import text

    async with pg_engine_slow.connect() as conn:
        sample_user = await conn.scalar(text("SELECT id FROM users LIMIT 1"))
        raw = await conn.scalar(
            text("EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = :u"),
            {"u": sample_user},
        )
    plan = json.loads(raw) if isinstance(raw, str) else raw
    assert plan[0]["Plan"]["Node Type"] == "Seq Scan"


async def test_reset_twice_is_idempotent(pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 9."""
    import asyncio

    from scripts.seed_fast import main
    from sqlalchemy import text

    for _ in range(2):
        await asyncio.to_thread(
            lambda: asyncio.run(
                main(
                    [
                        "--reset",
                        "--users",
                        "50",
                        "--orders",
                        "500",
                        "--order-items",
                        "2500",
                        "--products",
                        "10",
                    ]
                )
            )
        )

    async with pg_engine_fast.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM users")) == 50


async def test_rerun_without_reset_is_noop(pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 10."""
    import asyncio

    from scripts.seed_fast import main
    from sqlalchemy import text

    async with pg_engine_fast.connect() as conn:
        users_before = await conn.scalar(text("SELECT COUNT(*) FROM users"))

    await asyncio.to_thread(
        lambda: asyncio.run(main(["--users", "10", "--orders", "10", "--order-items", "10"]))
    )

    async with pg_engine_fast.connect() as conn:
        users_after = await conn.scalar(text("SELECT COUNT(*) FROM users"))

    assert users_before == users_after
