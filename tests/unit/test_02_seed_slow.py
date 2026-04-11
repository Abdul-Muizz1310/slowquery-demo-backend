"""S3 red: unit tests for spec 02 (seed_slow.py).

Pure helpers (row builders, arg parsing, self-guards) — no DB contact.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "seed_slow.py"


def test_build_user_rows_returns_unique_emails() -> None:
    """Spec 02 test 1."""
    from scripts._seed_common import build_user_rows

    rows = build_user_rows(n=100, seed=42)
    assert len(rows) == 100
    emails = [r.email for r in rows]
    assert len(set(emails)) == 100
    assert all(re.match(r"user_\d+@example\.com", e) for e in emails)


def test_build_order_rows_fk_integrity_and_skew() -> None:
    """Spec 02 test 2."""
    from scripts._seed_common import build_order_rows

    user_ids = [f"u{i}" for i in range(10)]
    rows = build_order_rows(user_ids=user_ids, n=1000, seed=42)
    assert len(rows) == 1000
    assert all(r.user_id in set(user_ids) for r in rows)
    # Skew check: top-1 user holds >2x the median number of orders.
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.user_id] = counts.get(r.user_id, 0) + 1
    sorted_counts = sorted(counts.values(), reverse=True)
    median = sorted_counts[len(sorted_counts) // 2]
    assert sorted_counts[0] > 2 * median


def test_build_order_item_rows_fk_integrity() -> None:
    """Spec 02 test 3."""
    from scripts._seed_common import build_order_item_rows

    order_ids = [f"o{i}" for i in range(50)]
    product_ids = [f"p{i}" for i in range(20)]
    rows = build_order_item_rows(order_ids=order_ids, product_ids=product_ids, n=5000, seed=42)
    assert len(rows) == 5000
    assert all(r.order_id in set(order_ids) for r in rows)
    assert all(r.product_id in set(product_ids) for r in rows)


def test_row_builders_are_pure_under_same_seed() -> None:
    """Spec 02 test 4."""
    from scripts._seed_common import build_user_rows

    h1 = hashlib.sha1(repr(build_user_rows(n=50, seed=7)).encode()).hexdigest()
    h2 = hashlib.sha1(repr(build_user_rows(n=50, seed=7)).encode()).hexdigest()
    assert h1 == h2


def test_parse_args_applies_override() -> None:
    """Spec 02 test 5."""
    from scripts.seed_slow import parse_args

    args = parse_args(["--users", "100"])
    assert args.users == 100
    assert args.orders > 0  # default preserved


def test_rejects_missing_database_url(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 11."""
    import asyncio

    from scripts.seed_slow import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main(["--users", "1", "--orders", "1", "--order-items", "1"]))
    assert exc.value.code == 1


def test_zero_users_rejected_by_argparse() -> None:
    """Spec 02 test 13."""
    from scripts.seed_slow import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--users", "0"])


def test_script_body_has_no_create_index_statement() -> None:
    """Spec 02 test 15: grep self-test."""
    assert SCRIPT_PATH.exists(), "seed_slow.py must be committed in S4"
    body = SCRIPT_PATH.read_text(encoding="utf-8")
    assert not re.search(r"CREATE\s+INDEX", body, re.IGNORECASE), (
        "seed_slow.py must never issue CREATE INDEX — the demo depends on it"
    )


def test_script_refuses_production_like_urls(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 02 test 16."""
    import asyncio

    from scripts.seed_slow import main

    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://u:p@prod.example.com:5432/main",
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(main(["--users", "1", "--orders", "1", "--order-items", "1"]))
    assert exc.value.code == 1
