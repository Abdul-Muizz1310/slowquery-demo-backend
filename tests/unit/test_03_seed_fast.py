"""S3 red: unit tests for spec 03 (seed_fast.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SEED_FAST = Path(__file__).resolve().parents[2] / "scripts" / "seed_fast.py"


def test_seed_fast_imports_helpers_from_seed_common() -> None:
    """Spec 03 test 1."""
    assert SEED_FAST.exists(), "seed_fast.py must be committed in S4"
    body = SEED_FAST.read_text(encoding="utf-8")
    assert "from scripts._seed_common import" in body or "from ._seed_common" in body
    # And doesn't redefine them.
    for name in ("build_user_rows", "build_order_rows", "build_order_item_rows"):
        assert f"def {name}" not in body, f"{name} must not be redefined in seed_fast.py"


def test_parse_args_matches_seed_slow_shape() -> None:
    """Spec 03 test 2."""
    from scripts.seed_fast import parse_args as parse_fast
    from scripts.seed_slow import parse_args as parse_slow

    a = parse_fast(["--users", "100"])
    b = parse_slow(["--users", "100"])
    assert a.users == b.users
    assert a.orders == b.orders
    assert a.order_items == b.order_items


def test_fast_indexes_constant_enumerates_three_indexes() -> None:
    """Spec 03 test 3."""
    from scripts.seed_fast import FAST_INDEXES

    assert isinstance(FAST_INDEXES, tuple)
    assert len(FAST_INDEXES) == 3
    joined = " ".join(FAST_INDEXES)
    assert "orders(user_id)" in joined
    assert "order_items(order_id)" in joined
    assert "order_items(product_id)" in joined


def test_rejects_missing_database_url_fast(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 11."""
    import asyncio

    from scripts.seed_fast import main

    monkeypatch.delenv("DATABASE_URL_FAST", raising=False)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main(["--users", "1", "--orders", "1", "--order-items", "1"]))
    assert exc.value.code == 1


def test_refuses_production_like_fast_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 03 test 14."""
    import asyncio

    from scripts.seed_fast import main

    monkeypatch.setenv(
        "DATABASE_URL_FAST",
        "postgresql+asyncpg://u:p@prod.example.com:5432/main",
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main(["--users", "1", "--orders", "1", "--order-items", "1"]))
    assert exc.value.code == 1


def test_create_index_appears_only_in_fast_indexes_constant() -> None:
    """Spec 03 test 15: every CREATE INDEX lives in the FAST_INDEXES tuple.

    The original S3 regex tried to extract the tuple body and count
    occurrences inside vs outside. It broke on SQL strings containing
    parentheses (``orders(user_id)``). The intent is simpler: the file
    must contain exactly ``len(FAST_INDEXES)`` CREATE INDEX statements,
    and every one of them must be the body of a FAST_INDEXES entry.
    """
    from scripts.seed_fast import FAST_INDEXES

    body = SEED_FAST.read_text(encoding="utf-8")
    total = len(re.findall(r"CREATE\s+INDEX", body, re.IGNORECASE))
    assert total == len(FAST_INDEXES) == 3, (
        f"expected 3 CREATE INDEX statements, found {total} in file, "
        f"{len(FAST_INDEXES)} in FAST_INDEXES"
    )
    for sql in FAST_INDEXES:
        assert "CREATE INDEX" in sql.upper(), (
            f"FAST_INDEXES entry must be a CREATE INDEX statement: {sql!r}"
        )


def test_no_extra_index_flag() -> None:
    """Spec 03 test 16."""
    from scripts.seed_fast import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--extra-index", "ix_foo"])
