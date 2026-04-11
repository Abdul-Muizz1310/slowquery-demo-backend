"""S3 red: integration tests for spec 02 (seed_slow.py)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_small_run_produces_exact_row_counts(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 6."""
    import asyncio

    from scripts.seed_slow import main
    from sqlalchemy import text

    await asyncio.to_thread(
        lambda: asyncio.run(
            main(
                [
                    "--reset",
                    "--users",
                    "100",
                    "--orders",
                    "1000",
                    "--order-items",
                    "5000",
                    "--products",
                    "20",
                ]
            )
        )
    )

    async with pg_engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM users")) == 100
        assert await conn.scalar(text("SELECT COUNT(*) FROM orders")) == 1000
        assert await conn.scalar(text("SELECT COUNT(*) FROM order_items")) == 5000
        assert await conn.scalar(text("SELECT COUNT(*) FROM products")) == 20


async def test_forbidden_indexes_absent_after_seed(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 7."""
    from sqlalchemy import text

    async with pg_engine.connect() as conn:
        orders_user_id = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM pg_indexes "
                "WHERE tablename = 'orders' AND indexdef LIKE '%user_id%'"
            )
        )
        items_order_id = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM pg_indexes "
                "WHERE tablename = 'order_items' AND indexdef LIKE '%order_id%'"
            )
        )
        items_product_id = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM pg_indexes "
                "WHERE tablename = 'order_items' AND indexdef LIKE '%product_id%'"
            )
        )
    assert orders_user_id == 0
    assert items_order_id == 0
    assert items_product_id == 0


async def test_rerun_without_reset_refuses(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 8."""
    import asyncio

    from scripts.seed_slow import main

    with pytest.raises(SystemExit) as exc:
        await asyncio.to_thread(
            lambda: asyncio.run(main(["--users", "10", "--orders", "10", "--order-items", "10"]))
        )
    assert exc.value.code == 1


async def test_reset_is_idempotent(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 9."""
    import asyncio

    from scripts.seed_slow import main
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

    async with pg_engine.connect() as conn:
        assert await conn.scalar(text("SELECT COUNT(*) FROM users")) == 50
        assert await conn.scalar(text("SELECT COUNT(*) FROM orders")) == 500


async def test_fk_integrity_end_to_end(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 10."""
    from sqlalchemy import text

    async with pg_engine.connect() as conn:
        orphan_orders = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM orders o "
                "LEFT JOIN users u ON o.user_id = u.id WHERE u.id IS NULL"
            )
        )
        orphan_items = await conn.scalar(
            text(
                "SELECT COUNT(*) FROM order_items oi "
                "LEFT JOIN orders o ON oi.order_id = o.id WHERE o.id IS NULL"
            )
        )
    assert orphan_orders == 0
    assert orphan_items == 0
