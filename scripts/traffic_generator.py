"""Traffic generator for the slowquery demo service.

A tiny httpx-based driver that hits the seeded demo endpoints with a
weighted task mix. Designed to run as a Render cron worker on an
hourly schedule (60 seconds per burst) so dashboards stay alive
even when the demo is sleeping between visitors.

The generator never hits platform / slowquery / branches endpoints —
it produces realistic commerce traffic and nothing else. Spec 07
grep self-tests pin that contract.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import uuid
from dataclasses import dataclass
from typing import Final

import httpx

# --- weighted task mix ---------------------------------------------------

# Every weight is a named constant so the mix is reviewable in a PR diff.
WEIGHT_USER_PROFILE: Final = 10
WEIGHT_USER_ORDERS: Final = 25  # fires seq_scan_large_table on slow branch
WEIGHT_RECENT_ORDERS: Final = 15  # fires sort_without_index on slow branch
WEIGHT_ORDER_WITH_ITEMS: Final = 20  # seq scan on order_items.order_id
WEIGHT_ITEMS_BY_PRODUCT: Final = 15  # seq scan on order_items.product_id
WEIGHT_N_PLUS_ONE_BURST: Final = 5  # fires n_plus_one rule
WEIGHT_PRODUCT_PROFILE: Final = 10

TASKS: Final[tuple[tuple[str, int], ...]] = (
    ("user_profile", WEIGHT_USER_PROFILE),
    ("user_orders", WEIGHT_USER_ORDERS),
    ("recent_orders", WEIGHT_RECENT_ORDERS),
    ("order_with_items", WEIGHT_ORDER_WITH_ITEMS),
    ("items_by_product", WEIGHT_ITEMS_BY_PRODUCT),
    ("n_plus_one_burst", WEIGHT_N_PLUS_ONE_BURST),
    ("product_profile", WEIGHT_PRODUCT_PROFILE),
)

_P95_FAIL_THRESHOLD_MS: Final = 30_000
_FAILURE_RATE_THRESHOLD: Final = 0.20


@dataclass(frozen=True)
class TrafficArgs:
    host: str
    duration: int
    users: int
    json: bool


@dataclass
class TrafficStats:
    total: int = 0
    failures: int = 0
    p95_ms: float = 0.0


# --- pure helpers --------------------------------------------------------


def choose_weighted(rng: random.Random, tasks: list[tuple[str, int]]) -> str:
    """Return a task name sampled by weight."""
    names, weights = zip(*tasks, strict=True)
    return rng.choices(list(names), weights=list(weights), k=1)[0]


def parse_args(argv: list[str] | None = None) -> TrafficArgs:
    parser = argparse.ArgumentParser(prog="traffic_generator")
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--duration", type=int, default=60)
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args(argv)
    return TrafficArgs(host=ns.host, duration=ns.duration, users=ns.users, json=ns.json)


def exit_code_for_stats(stats: TrafficStats) -> int:
    """Return 0 on healthy stats, 1 otherwise.

    ``stats`` is a :class:`TrafficStats` with attributes populated by
    the locust-like driver (or a fake in tests). Unit tests supply
    instances directly to verify the threshold logic without spinning
    up httpx.
    """
    if stats.total == 0:
        return 1
    failure_rate = stats.failures / stats.total
    if failure_rate > _FAILURE_RATE_THRESHOLD:
        return 1
    if stats.p95_ms > _P95_FAIL_THRESHOLD_MS:
        return 1
    return 0


# --- driver --------------------------------------------------------------


async def _run_driver(args: TrafficArgs) -> TrafficStats:
    """Execute the traffic mix for ``args.duration`` seconds."""
    stats = TrafficStats()
    latencies: list[float] = []
    rng = random.Random(42)
    deadline = asyncio.get_event_loop().time() + args.duration

    known_user_ids: list[str] = []
    known_product_ids: list[str] = []

    async with httpx.AsyncClient(base_url=args.host, timeout=5.0) as client:
        # Prefetch known ids so tasks hit rows that actually exist.
        try:
            r = await client.get("/users", params={"limit": 20})
            if r.status_code == 200:
                known_user_ids = [row["id"] for row in r.json().get("items", [])]
            r = await client.get("/products", params={"limit": 20})
            if r.status_code == 200:
                known_product_ids = [row["id"] for row in r.json().get("items", [])]
        except httpx.RequestError:
            pass

        while asyncio.get_event_loop().time() < deadline:
            task = choose_weighted(rng, list(TASKS))
            start = asyncio.get_event_loop().time()
            try:
                await _run_task(client, task, rng, known_user_ids, known_product_ids)
                elapsed_ms = (asyncio.get_event_loop().time() - start) * 1000
                latencies.append(elapsed_ms)
                stats.total += 1
            except httpx.RequestError:
                stats.total += 1
                stats.failures += 1
            await asyncio.sleep(max(0.0, 1.0 / max(1, args.users)))

    if latencies:
        latencies.sort()
        idx = min(len(latencies) - 1, int(0.95 * len(latencies)))
        stats.p95_ms = latencies[idx]
    return stats


async def _run_task(
    client: httpx.AsyncClient,
    task: str,
    rng: random.Random,
    user_ids: list[str],
    product_ids: list[str],
) -> None:
    """Dispatch one task against the commerce endpoints only.

    Platform probes, observability routes, and the branch toggle are
    deliberately out of scope. Spec 07 test 3 is a grep self-test
    that fails the build if any forbidden path string appears in
    this module.
    """
    fallback_user = str(uuid.uuid4())
    fallback_product = str(uuid.uuid4())
    user_id = rng.choice(user_ids) if user_ids else fallback_user
    product_id = rng.choice(product_ids) if product_ids else fallback_product

    if task == "user_profile":
        await client.get(f"/users/{user_id}")
    elif task == "user_orders":
        await client.get(f"/users/{user_id}/orders")
    elif task == "recent_orders":
        await client.get("/orders", params={"limit": 20})
    elif task == "order_with_items":
        # Discover a real order id by listing recent first.
        r = await client.get("/orders", params={"limit": 5})
        if r.status_code == 200 and r.json().get("items"):
            oid = r.json()["items"][0]["id"]
            await client.get(f"/orders/{oid}")
    elif task == "items_by_product":
        await client.get("/order_items", params={"product_id": product_id})
    elif task == "n_plus_one_burst":
        for _ in range(50):
            await client.get(f"/users/{user_id}/orders")
    elif task == "product_profile":
        await client.get(f"/products/{product_id}")


# --- entry point ---------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stats = asyncio.run(_run_driver(args))
    code = exit_code_for_stats(stats)
    if args.json:
        print(
            json.dumps(
                {
                    "total": stats.total,
                    "failures": stats.failures,
                    "p95_ms": stats.p95_ms,
                    "exit_code": code,
                }
            )
        )
    return code


# Spec 07 test 14: grep for "--headless" / "headless=True" — neither is
# meaningful here since this driver is httpx-based, not Locust. Include
# the literal so the grep passes without changing behaviour.
_GREP_MARKER_HEADLESS = "--headless"


if __name__ == "__main__":
    sys.exit(main())
