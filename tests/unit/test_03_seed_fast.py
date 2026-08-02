"""S3 red: unit tests for spec 03 (seed_fast.py)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SEED_FAST = Path(__file__).resolve().parents[2] / "scripts" / "seed_fast.py"


def test_seed_fast_reuses_the_shared_row_builders() -> None:
    """Spec 03 test 1: the row builders are *the same objects* as seed_slow's.

    Spec 03 invariant 5 (identical row identities across both branches) only
    holds if both scripts share one deterministic generator. Asserting object
    identity proves reuse; the earlier version grepped the source for an import
    line and a missing ``def``, which a copy-pasted lambda or a re-export would
    have satisfied without actually sharing the implementation.
    """
    from scripts import _seed_common, seed_fast, seed_slow

    for name in ("build_user_rows", "build_order_rows", "build_order_item_rows"):
        shared = getattr(_seed_common, name)
        assert getattr(seed_fast, name, shared) is shared, f"seed_fast redefines {name}"
        assert getattr(seed_slow, name, shared) is shared, f"seed_slow redefines {name}"


def test_parse_args_matches_seed_slow_shape() -> None:
    """Spec 03 test 2."""
    from scripts.seed_fast import parse_args as parse_fast
    from scripts.seed_slow import parse_args as parse_slow

    a = parse_fast(["--users", "100"])
    b = parse_slow(["--users", "100"])
    assert a.users == b.users
    assert a.orders == b.orders
    assert a.order_items == b.order_items


def test_fast_indexes_constant_enumerates_four_indexes() -> None:
    """Spec 03 test 3.

    Four indexes ship on the fast branch. ``orders(created_at)`` backs
    the headlined ``GET /orders`` (``ORDER BY created_at DESC``) query;
    ``orders(user_id)`` backs ``GET /users/{id}/orders``; the two
    ``order_items`` foreign-key indexes back the join paths.
    """
    from scripts.seed_fast import FAST_INDEXES

    assert isinstance(FAST_INDEXES, tuple)
    assert len(FAST_INDEXES) == 4
    joined = " ".join(FAST_INDEXES)
    assert "orders(user_id)" in joined
    assert "orders(created_at)" in joined
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
    assert total == len(FAST_INDEXES) == 4, (
        f"expected 4 CREATE INDEX statements, found {total} in file, "
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
