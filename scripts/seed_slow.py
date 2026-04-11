"""Seed the ``slowquery`` Neon branch with the 1M-row commerce dataset.

See ``docs/specs/02-seed-slow.md`` for the full contract. The defining
feature of this script is what it does NOT do: it never creates an
index on the three columns the slow-path demo depends on
(``orders.user_id``, ``order_items.order_id``,
``order_items.product_id``). Spec 02 test 15 is a grep self-test
that fails the build if any index-creation DDL slips into this file.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass

from scripts._seed_common import (
    build_order_item_rows,
    build_order_rows,
    build_product_rows,
    build_user_rows,
)

_DEFAULT_USERS = 10_000
_DEFAULT_PRODUCTS = 2_000
_DEFAULT_ORDERS = 1_000_000
_DEFAULT_ORDER_ITEMS = 5_000_000
_DEFAULT_SEED = 42


@dataclass(frozen=True)
class SeedArgs:
    users: int
    products: int
    orders: int
    order_items: int
    seed: int
    reset: bool


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parse_args(argv: list[str] | None = None) -> SeedArgs:
    parser = argparse.ArgumentParser(
        prog="seed_slow",
        description="Populate the slowquery Neon branch with the demo dataset.",
    )
    parser.add_argument("--users", type=_positive_int, default=_DEFAULT_USERS)
    parser.add_argument("--products", type=_positive_int, default=_DEFAULT_PRODUCTS)
    parser.add_argument("--orders", type=_positive_int, default=_DEFAULT_ORDERS)
    parser.add_argument(
        "--order-items", type=_positive_int, default=_DEFAULT_ORDER_ITEMS, dest="order_items"
    )
    parser.add_argument("--seed", type=int, default=_DEFAULT_SEED)
    parser.add_argument("--reset", action="store_true")
    ns = parser.parse_args(argv)
    return SeedArgs(
        users=ns.users,
        products=ns.products,
        orders=ns.orders,
        order_items=ns.order_items,
        seed=ns.seed,
        reset=ns.reset,
    )


_SAFE_HOST_MARKERS = ("slowquery", "localhost", "127.0.0.1")


def _is_safe_url(url: str) -> bool:
    return any(marker in url for marker in _SAFE_HOST_MARKERS)


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    url = os.environ.get("DATABASE_URL")
    if not url:
        _die("DATABASE_URL is required (point at the slowquery Neon branch)")
    assert url is not None
    if not _is_safe_url(url):
        _die(
            "refusing to seed a production-looking DATABASE_URL; "
            "the URL must contain 'slowquery', 'localhost', or '127.0.0.1'"
        )

    # Real seed work (asyncpg bulk COPY) lives behind the env guards above
    # so unit tests can exercise the CLI + safety checks without an asyncpg
    # import in the hot path. Integration tests (spec 02 tests 6-10) run the
    # full pipeline against a Testcontainers Postgres.
    await _run_seed(url, args)


async def _run_seed(url: str, args: SeedArgs) -> None:
    import asyncpg  # local import — keeps unit tests fast and cheap

    conn = await asyncpg.connect(dsn=url)
    try:
        if args.reset:
            await conn.execute(
                "TRUNCATE order_items, orders, products, users RESTART IDENTITY CASCADE"
            )
        else:
            existing = await conn.fetchval("SELECT COUNT(*) FROM users")
            if existing and existing > 0:
                _die(
                    f"refusing to seed: users table has {existing} rows; "
                    "pass --reset to wipe and re-seed"
                )

        user_rows = build_user_rows(n=args.users, seed=args.seed)
        product_rows = build_product_rows(n=args.products, seed=args.seed)

        user_ids = [str(uuid.uuid4()) for _ in user_rows]
        product_ids = [str(uuid.uuid4()) for _ in product_rows]

        async with conn.transaction():
            await conn.copy_records_to_table(
                "users",
                records=[
                    (uid, r.email, r.full_name) for uid, r in zip(user_ids, user_rows, strict=True)
                ],
                columns=["id", "email", "full_name"],
            )
            await conn.copy_records_to_table(
                "products",
                records=[
                    (pid, r.sku, r.name, r.price_cents)
                    for pid, r in zip(product_ids, product_rows, strict=True)
                ],
                columns=["id", "sku", "name", "price_cents"],
            )

            order_rows = build_order_rows(user_ids=user_ids, n=args.orders, seed=args.seed)
            order_ids = [str(uuid.uuid4()) for _ in order_rows]
            await conn.copy_records_to_table(
                "orders",
                records=[
                    (oid, r.user_id, r.status, r.total_cents)
                    for oid, r in zip(order_ids, order_rows, strict=True)
                ],
                columns=["id", "user_id", "status", "total_cents"],
            )

            item_rows = build_order_item_rows(
                order_ids=order_ids,
                product_ids=product_ids,
                n=args.order_items,
                seed=args.seed,
            )
            await conn.copy_records_to_table(
                "order_items",
                records=[
                    (
                        str(uuid.uuid4()),
                        r.order_id,
                        r.product_id,
                        r.quantity,
                        r.unit_price_cents,
                    )
                    for r in item_rows
                ],
                columns=[
                    "id",
                    "order_id",
                    "product_id",
                    "quantity",
                    "unit_price_cents",
                ],
            )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
