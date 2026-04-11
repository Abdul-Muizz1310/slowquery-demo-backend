"""Seed the ``slowquery-fast`` Neon branch with the same rows + 3 indexes.

Row generation is delegated to ``scripts/_seed_common`` so this script
and ``seed_slow.py`` cannot drift. The three missing indexes on the
slow branch are materialized here out of the ``FAST_INDEXES`` constant
(Spec 03 invariant 3). ``--reset`` truncates and re-inserts; without
``--reset`` on a non-empty database the script is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Final

from scripts._seed_common import (
    build_order_item_rows,
    build_order_rows,
    build_product_rows,
    build_user_rows,
)

FAST_INDEXES: Final[tuple[str, ...]] = (
    "CREATE INDEX IF NOT EXISTS ix_orders_user_id ON orders(user_id)",
    "CREATE INDEX IF NOT EXISTS ix_order_items_order_id ON order_items(order_id)",
    "CREATE INDEX IF NOT EXISTS ix_order_items_product_id ON order_items(product_id)",
)

_DEFAULT_USERS = 10_000
_DEFAULT_PRODUCTS = 2_000
_DEFAULT_ORDERS = 1_000_000
_DEFAULT_ORDER_ITEMS = 5_000_000
_DEFAULT_SEED = 42

_SAFE_HOST_MARKERS = ("slowquery-fast", "localhost", "127.0.0.1", "neon.tech")


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
        prog="seed_fast",
        description="Populate the slowquery-fast Neon branch with the demo dataset + indexes.",
    )
    parser.add_argument("--users", type=_positive_int, default=_DEFAULT_USERS)
    parser.add_argument("--products", type=_positive_int, default=_DEFAULT_PRODUCTS)
    parser.add_argument("--orders", type=_positive_int, default=_DEFAULT_ORDERS)
    parser.add_argument(
        "--order-items",
        type=_positive_int,
        default=_DEFAULT_ORDER_ITEMS,
        dest="order_items",
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


def _is_safe_url(url: str) -> bool:
    return any(marker in url for marker in _SAFE_HOST_MARKERS)


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


async def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    url = os.environ.get("DATABASE_URL_FAST")
    if not url:
        _die("DATABASE_URL_FAST is required (point at the slowquery-fast Neon branch)")
    assert url is not None
    if not _is_safe_url(url):
        _die(
            "refusing to seed a production-looking DATABASE_URL_FAST; "
            "the URL must contain 'slowquery-fast', 'localhost', or '127.0.0.1'"
        )

    await _run_seed(url, args)


async def _run_seed(url: str, args: SeedArgs) -> None:
    import asyncpg

    from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

    conn = await asyncpg.connect(dsn=to_raw_asyncpg_dsn(url))
    try:
        if args.reset:
            await conn.execute(
                "TRUNCATE order_items, orders, products, users RESTART IDENTITY CASCADE"
            )
        else:
            existing = await conn.fetchval("SELECT COUNT(*) FROM users")
            if existing and existing > 0:
                # Non-reset rerun after a successful seed is a no-op.
                return

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

        # Index creation runs last: building indexes against an already-
        # populated table is dramatically faster than indexing during a
        # bulk insert (Postgres can do a single sort instead of many
        # tree rebalances).
        for stmt in FAST_INDEXES:
            await conn.execute(stmt)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
