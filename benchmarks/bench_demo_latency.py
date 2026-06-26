"""Measure the slow-vs-fast latency delta for the headlined demo query.

This is the reproducible artifact behind the README's "switching the
branch turns seq scans into index scans" claim. It runs the
``GET /users/{id}/orders`` query — ``SELECT ... FROM orders WHERE
user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2`` — ``M`` times
against the slow branch and ``M`` times against the fast branch, then
prints p50 / p95 / p99 per branch.

The numbers are environment- and seed-dependent. A 100k-order seed on
Neon Free shows a modest delta; the full 1M-order seed shows a large
one (see ``docs/DEVIATIONS.md`` §1 / §4). The point of this script is
to let anyone measure their own figures rather than trust a number in
a README.

Usage::

    DATABASE_URL=postgresql://.../slowquery \\
    DATABASE_URL_FAST=postgresql://.../slowquery-fast \\
      uv run python -m benchmarks.bench_demo_latency --iterations 200

or with positional URLs::

    uv run python -m benchmarks.bench_demo_latency \\
      --slow-url postgresql://.../slowquery \\
      --fast-url postgresql://.../slowquery-fast

The module imports cleanly without a database — the connection is only
opened inside :func:`main`.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg

# The query under test mirrors ``order_repository.list_for_user`` exactly
# so the benchmark measures the same plan the live endpoint produces.
_QUERY = (
    "SELECT id, user_id, status, total_cents, created_at "
    "FROM orders WHERE user_id = $1 "
    "ORDER BY created_at DESC, id DESC LIMIT $2"
)

_DEFAULT_ITERATIONS = 200
_DEFAULT_LIMIT = 20


@dataclass(frozen=True)
class BenchArgs:
    slow_url: str
    fast_url: str
    iterations: int
    limit: int


@dataclass(frozen=True)
class BranchResult:
    branch: str
    samples_ms: tuple[float, ...]

    @property
    def p50_ms(self) -> float:
        return _percentile(self.samples_ms, 0.50)

    @property
    def p95_ms(self) -> float:
        return _percentile(self.samples_ms, 0.95)

    @property
    def p99_ms(self) -> float:
        return _percentile(self.samples_ms, 0.99)


def _percentile(samples: tuple[float, ...], pct: float) -> float:
    """Linear-interpolated percentile over a non-empty sample tuple."""
    if not samples:
        raise ValueError("samples must be non-empty")
    if not 0.0 <= pct <= 1.0:
        raise ValueError("pct must be in [0.0, 1.0]")
    ordered = sorted(samples)
    n = len(ordered)
    if n == 1:
        return ordered[0]
    rank = pct * (n - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    weight = rank - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def parse_args(argv: list[str] | None = None) -> BenchArgs:
    """Parse CLI args, falling back to env vars for the two branch URLs.

    Raises:
        SystemExit: if either URL is missing (argparse error path).
    """
    parser = argparse.ArgumentParser(
        prog="bench_demo_latency",
        description=(
            "Measure p50/p95/p99 of the GET /users/{id}/orders query against "
            "the slow and fast Neon branches."
        ),
    )
    parser.add_argument(
        "--slow-url",
        default=os.environ.get("DATABASE_URL"),
        help="slow-branch URL (defaults to $DATABASE_URL)",
    )
    parser.add_argument(
        "--fast-url",
        default=os.environ.get("DATABASE_URL_FAST"),
        help="fast-branch URL (defaults to $DATABASE_URL_FAST)",
    )
    parser.add_argument("--iterations", type=_positive_int, default=_DEFAULT_ITERATIONS)
    parser.add_argument("--limit", type=_positive_int, default=_DEFAULT_LIMIT)
    ns = parser.parse_args(argv)

    if not ns.slow_url:
        parser.error("slow-branch URL required: pass --slow-url or set DATABASE_URL")
    if not ns.fast_url:
        parser.error("fast-branch URL required: pass --fast-url or set DATABASE_URL_FAST")

    return BenchArgs(
        slow_url=ns.slow_url,
        fast_url=ns.fast_url,
        iterations=ns.iterations,
        limit=ns.limit,
    )


async def _sample_user_id(conn: asyncpg.Connection) -> object:
    """Return one existing user id, or raise if the table is empty."""
    user_id = await conn.fetchval("SELECT id FROM users LIMIT 1")
    if user_id is None:
        raise RuntimeError("no rows in users; seed the branch before benchmarking")
    return user_id


async def _bench_branch(branch: str, url: str, args: BenchArgs) -> BranchResult:
    """Run the query ``args.iterations`` times against one branch."""
    import asyncpg

    from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

    conn = await asyncpg.connect(dsn=to_raw_asyncpg_dsn(url))
    try:
        user_id = await _sample_user_id(conn)
        # Warm the connection / planner so the first sample isn't an outlier.
        await conn.fetch(_QUERY, user_id, args.limit)

        samples: list[float] = []
        for _ in range(args.iterations):
            start = time.perf_counter()
            await conn.fetch(_QUERY, user_id, args.limit)
            samples.append((time.perf_counter() - start) * 1000.0)
    finally:
        await conn.close()

    return BranchResult(branch=branch, samples_ms=tuple(samples))


def _format_result(result: BranchResult) -> str:
    return (
        f"{result.branch:<5} "
        f"n={len(result.samples_ms):<5} "
        f"p50={result.p50_ms:8.2f}ms "
        f"p95={result.p95_ms:8.2f}ms "
        f"p99={result.p99_ms:8.2f}ms"
    )


async def run(args: BenchArgs) -> tuple[BranchResult, BranchResult]:
    """Benchmark both branches and return (slow_result, fast_result)."""
    slow = await _bench_branch("slow", args.slow_url, args)
    fast = await _bench_branch("fast", args.fast_url, args)
    return slow, fast


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    slow, fast = await run(args)

    print(f"query: {_QUERY}")
    print(f"iterations per branch: {args.iterations}")
    print(_format_result(slow))
    print(_format_result(fast))

    if fast.p95_ms < slow.p95_ms:
        delta = slow.p95_ms - fast.p95_ms
        print(f"p95 improved by {delta:.2f}ms switching slow -> fast")
    else:
        print("p95 did not improve; seed may be too small to surface the index win")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
