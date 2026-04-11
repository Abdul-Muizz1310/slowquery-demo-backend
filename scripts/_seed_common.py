"""Pure row-generation helpers shared by ``seed_slow.py`` and ``seed_fast.py``.

No I/O, no asyncpg, no argparse. Every function takes a seed and is
deterministic under that seed. The two seed scripts are thin glue around
these helpers — Spec 03 invariant 1 requires that ``seed_fast`` imports
these functions rather than redefining them so the two branches can't
drift.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

__all__ = [
    "OrderItemRow",
    "OrderRow",
    "ProductRow",
    "UserRow",
    "build_order_item_rows",
    "build_order_rows",
    "build_product_rows",
    "build_user_rows",
]


@dataclass(frozen=True)
class UserRow:
    email: str
    full_name: str


@dataclass(frozen=True)
class ProductRow:
    sku: str
    name: str
    price_cents: int


@dataclass(frozen=True)
class OrderRow:
    user_id: str
    status: str
    total_cents: int


@dataclass(frozen=True)
class OrderItemRow:
    order_id: str
    product_id: str
    quantity: int
    unit_price_cents: int


_FIRST_NAMES = ("Ada", "Alan", "Grace", "Linus", "Barbara", "Edsger", "Ken", "Margaret")
_LAST_NAMES = (
    "Lovelace",
    "Turing",
    "Hopper",
    "Torvalds",
    "Liskov",
    "Dijkstra",
    "Thompson",
    "Hamilton",
)
_STATUSES = ("pending", "paid", "shipped", "cancelled")


def build_user_rows(*, n: int, seed: int = 42) -> list[UserRow]:
    """Return ``n`` deterministic user rows with unique emails."""
    rng = random.Random(seed)
    rows: list[UserRow] = []
    for i in range(n):
        first = _FIRST_NAMES[rng.randrange(len(_FIRST_NAMES))]
        last = _LAST_NAMES[rng.randrange(len(_LAST_NAMES))]
        rows.append(UserRow(email=f"user_{i}@example.com", full_name=f"{first} {last}"))
    return rows


def build_product_rows(*, n: int, seed: int = 42) -> list[ProductRow]:
    """Return ``n`` deterministic product rows with unique SKUs."""
    rng = random.Random(seed + 1)
    rows: list[ProductRow] = []
    for i in range(n):
        rows.append(
            ProductRow(
                sku=f"SKU-{i:06d}",
                name=f"Product {i}",
                price_cents=rng.randint(500, 50_000),
            )
        )
    return rows


def build_order_rows(
    *,
    user_ids: list[str],
    n: int,
    seed: int = 42,
) -> list[OrderRow]:
    """Return ``n`` deterministic order rows with a skewed user distribution.

    Uses a 1/rank weighting so the top-1 user holds dramatically more
    orders than the median — enough for the slow branch's Seq Scan cost
    on ``orders.user_id`` to dominate query time. Spec 02 test 2 asserts
    the top-1 count is more than 2x the median.
    """
    if not user_ids:
        raise ValueError("user_ids must be non-empty")
    rng = random.Random(seed + 2)
    weights = [1.0 / (i + 1) for i in range(len(user_ids))]
    rows: list[OrderRow] = []
    for _ in range(n):
        user_id = rng.choices(user_ids, weights=weights, k=1)[0]
        status = _STATUSES[rng.randrange(len(_STATUSES))]
        rows.append(
            OrderRow(
                user_id=user_id,
                status=status,
                total_cents=rng.randint(100, 1_000_000),
            )
        )
    return rows


def build_order_item_rows(
    *,
    order_ids: list[str],
    product_ids: list[str],
    n: int,
    seed: int = 42,
) -> list[OrderItemRow]:
    """Return ``n`` deterministic order-item rows, FK-integrity preserved."""
    if not order_ids:
        raise ValueError("order_ids must be non-empty")
    if not product_ids:
        raise ValueError("product_ids must be non-empty")
    rng = random.Random(seed + 3)
    rows: list[OrderItemRow] = []
    for _ in range(n):
        rows.append(
            OrderItemRow(
                order_id=order_ids[rng.randrange(len(order_ids))],
                product_id=product_ids[rng.randrange(len(product_ids))],
                quantity=rng.randint(1, 5),
                unit_price_cents=rng.randint(100, 100_000),
            )
        )
    return rows
