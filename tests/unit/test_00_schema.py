"""S3 red: unit tests for spec 00 (database schema).

Every enumerated case from ``docs/specs/00-schema.md`` has one test function
here. Imports happen inside test bodies so each test fails independently on
a missing implementation rather than collapsing the whole file at collection
time. S4 lands the target modules and these tests flip green.
"""

from __future__ import annotations

import re
from pathlib import Path

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "users",
        "products",
        "orders",
        "order_items",
        "query_fingerprints",
        "query_samples",
        "explain_plans",
        "suggestions",
    }
)


def test_metadata_contains_eight_expected_tables() -> None:
    """Spec 00 test 1."""
    from slowquery_demo.models.base import Base

    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


def test_orders_fk_user_id_cascades() -> None:
    """Spec 00 test 2."""
    from slowquery_demo.models.base import Base

    orders = Base.metadata.tables["orders"]
    fks = [fk for fk in orders.foreign_keys if fk.parent.name == "user_id"]
    assert len(fks) == 1, "orders.user_id must have exactly one FK"
    fk = fks[0]
    assert fk.column.table.name == "users"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_order_items_has_two_cascading_fks() -> None:
    """Spec 00 test 3."""
    from slowquery_demo.models.base import Base

    order_items = Base.metadata.tables["order_items"]
    fk_cols = {fk.parent.name for fk in order_items.foreign_keys}
    assert fk_cols == {"order_id", "product_id"}
    for fk in order_items.foreign_keys:
        assert fk.ondelete == "CASCADE", f"{fk.parent.name} must cascade"


def test_order_status_enum_has_four_members() -> None:
    """Spec 00 test 4."""
    from slowquery_demo.models.base import Base
    from sqlalchemy.dialects.postgresql import ENUM

    orders = Base.metadata.tables["orders"]
    status_col = orders.c.status
    assert isinstance(status_col.type, ENUM)
    assert set(status_col.type.enums) == {"pending", "paid", "shipped", "cancelled"}


def test_check_constraints_are_present() -> None:
    """Spec 00 test 5."""
    from slowquery_demo.models.base import Base
    from sqlalchemy import CheckConstraint

    checks: dict[str, list[str]] = {}
    for table_name, col_name in [
        ("products", "price_cents"),
        ("orders", "total_cents"),
        ("order_items", "quantity"),
        ("order_items", "unit_price_cents"),
    ]:
        table = Base.metadata.tables[table_name]
        matching = [
            str(c.sqltext)
            for c in table.constraints
            if isinstance(c, CheckConstraint) and col_name in str(c.sqltext)
        ]
        assert matching, f"{table_name}.{col_name} must have a CHECK constraint"
        checks[f"{table_name}.{col_name}"] = matching


def test_query_fingerprints_id_is_short_string_pk() -> None:
    """Spec 00 test 6."""
    from slowquery_demo.models.base import Base
    from sqlalchemy import String

    fingerprints = Base.metadata.tables["query_fingerprints"]
    id_col = fingerprints.c.id
    assert id_col.primary_key is True
    assert isinstance(id_col.type, String)
    # Library's fingerprint() returns sha1[:16]; we allow up to 32 as a safe upper bound.
    assert id_col.type.length is not None and id_col.type.length <= 32


def test_explain_plans_fingerprint_id_is_pk() -> None:
    """Spec 00 test 7."""
    from slowquery_demo.models.base import Base

    explain_plans = Base.metadata.tables["explain_plans"]
    pk_cols = {c.name for c in explain_plans.primary_key.columns}
    assert pk_cols == {"fingerprint_id"}, "explain_plans PK must be fingerprint_id alone"


def test_alembic_env_rejects_missing_database_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 00 test 14: alembic env.py raises when DATABASE_URL is unset."""
    import sys

    from alembic import context

    # Remove DATABASE_URL from the environment and prove the getter fails loud.
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # Re-import the env module to get a fresh `_get_url` reference that reads
    # the current environment.
    sys.modules.pop("alembic_env_under_test", None)
    import importlib.util

    env_path = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    spec = importlib.util.spec_from_file_location("alembic_env_under_test", env_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # env.py calls ``context.config`` at import which only works inside an
    # alembic run, so the import itself is expected to fail or the module to
    # no-op. We exercise the private ``_get_url`` helper directly instead.
    import contextlib

    with contextlib.suppress(Exception):
        spec.loader.exec_module(module)

    # After a successful land of the real env.py, this helper exists and raises.
    import pytest
    from alembic_env_under_test import _get_url  # type: ignore[import-not-found]

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _get_url()

    # Keep the alembic context reference used — silences the unused-import warning.
    _ = context


def test_migration_does_not_create_forbidden_indexes() -> None:
    """Spec 00 test 16: guard against a refactor silently fixing the slow path."""
    migration_path = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0001_initial.py"
    )
    assert migration_path.exists(), "0001_initial.py must be committed in S4"
    body = migration_path.read_text(encoding="utf-8")

    forbidden = [
        (r"orders", r"user_id"),
        (r"order_items", r"order_id"),
        (r"order_items", r"product_id"),
    ]
    for table, col in forbidden:
        pattern = re.compile(rf"create_index\([^)]*{table}[^)]*{col}", re.IGNORECASE | re.DOTALL)
        assert not pattern.search(body), (
            f"migration 0001 creates an index on {table}.{col} — "
            "the slow-path demo depends on its absence"
        )
