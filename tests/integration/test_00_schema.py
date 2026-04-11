"""S3 red: integration tests for spec 00 (database schema).

These tests need a real Postgres via Testcontainers and are filtered out of
the default CI run via ``-m "not integration"``. S4 will build the
conftest fixtures and flip the gate.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_alembic_upgrade_creates_every_table(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 8."""
    from sqlalchemy import inspect

    from slowquery_demo.models.base import Base  # noqa: F401 — side-effect import

    async with pg_engine.connect() as conn:
        tables = await conn.run_sync(lambda sync: sorted(inspect(sync).get_table_names()))

    expected = {
        "users",
        "products",
        "orders",
        "order_items",
        "query_fingerprints",
        "query_samples",
        "explain_plans",
        "suggestions",
        "alembic_version",
    }
    assert expected.issubset(set(tables))


async def test_alembic_downgrade_drops_tables_and_enum(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 9."""
    from sqlalchemy import inspect, text

    async with pg_engine.connect() as conn:
        # After downgrade (performed by fixture), no demo tables remain.
        tables = await conn.run_sync(lambda sync: inspect(sync).get_table_names())
        assert "orders" not in tables
        # The order_status enum type must also be gone.
        result = await conn.execute(text("SELECT 1 FROM pg_type WHERE typname = 'order_status'"))
        assert result.scalar() is None


async def test_autogenerate_reports_empty_diff(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 10: target_metadata matches reality."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from slowquery_demo.models.base import Base

    async with pg_engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    assert diff == [], f"unexpected diff: {diff}"


async def test_invalid_order_status_rejected(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 11."""
    from sqlalchemy import text
    from sqlalchemy.exc import DataError

    async with pg_engine.begin() as conn:
        with pytest.raises(DataError):
            await conn.execute(
                text(
                    "INSERT INTO orders (id, user_id, status, total_cents) "
                    "VALUES (gen_random_uuid(), gen_random_uuid(), 'expired', 0)"
                )
            )


async def test_user_delete_cascades_to_orders_and_items(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 12."""
    from sqlalchemy import text

    async with pg_engine.begin() as conn:
        user_id = await conn.scalar(
            text(
                "INSERT INTO users (id, email, full_name) "
                "VALUES (gen_random_uuid(), 'casc@x.test', 'C') RETURNING id"
            )
        )
        order_id = await conn.scalar(
            text(
                "INSERT INTO orders (id, user_id, status, total_cents) "
                "VALUES (gen_random_uuid(), :u, 'pending', 100) RETURNING id"
            ),
            {"u": user_id},
        )
        product_id = await conn.scalar(
            text(
                "INSERT INTO products (id, sku, name, price_cents) "
                "VALUES (gen_random_uuid(), 'sku-casc', 'p', 100) RETURNING id"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO order_items (id, order_id, product_id, quantity, unit_price_cents) "
                "VALUES (gen_random_uuid(), :o, :p, 1, 100)"
            ),
            {"o": order_id, "p": product_id},
        )

    async with pg_engine.begin() as conn:
        await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})

    async with pg_engine.connect() as conn:
        remaining_orders = await conn.scalar(
            text("SELECT COUNT(*) FROM orders WHERE user_id = :u"), {"u": user_id}
        )
        remaining_items = await conn.scalar(
            text("SELECT COUNT(*) FROM order_items WHERE order_id = :o"), {"o": order_id}
        )
    assert remaining_orders == 0
    assert remaining_items == 0


async def test_products_price_cents_zero_rejected(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 13."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    async with pg_engine.begin() as conn:
        with pytest.raises(IntegrityError):
            await conn.execute(
                text(
                    "INSERT INTO products (id, sku, name, price_cents) "
                    "VALUES (gen_random_uuid(), 'sku-zero', 'p', 0)"
                )
            )


async def test_dropping_enum_with_referencing_rows_fails(pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 15: downgrade path must drop tables before the enum type."""
    from sqlalchemy import text
    from sqlalchemy.exc import DatabaseError

    async with pg_engine.begin() as conn:
        with pytest.raises(DatabaseError):
            await conn.execute(text("DROP TYPE order_status"))
