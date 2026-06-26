# Benchmarks

This directory holds the reproducible artifact behind the README's
slow-vs-fast latency claim. There is no checked-in "1200ms -> 18ms"
number anywhere in this repo presented as a measured fact, because that
figure was never produced by a committed benchmark. Instead, you run the
script below against your own seeded branches and read off your own
numbers.

## What it measures

`bench_demo_latency.py` times the headlined demo query -- the one behind
`GET /users/{id}/orders`:

```sql
SELECT id, user_id, status, total_cents, created_at
FROM orders
WHERE user_id = $1
ORDER BY created_at DESC, id DESC
LIMIT $2
```

It runs that query `--iterations` times (default 200) against the slow
branch and again against the fast branch, then prints p50 / p95 / p99
per branch. The two branches must be row-identical (same seed) so the
only difference is indexing.

- On the **slow** branch there is no index on `orders.user_id` (or
  `orders.created_at`), so Postgres does a sequential scan plus a sort.
- On the **fast** branch `seed_fast.py`'s `FAST_INDEXES` creates
  `ix_orders_user_id` and `ix_orders_created_at` (plus the two
  `order_items` foreign-key indexes), so the planner can use an index
  scan and skip the sort.

## How to run

Seed both branches with matching flags first (see the repo README), then:

```bash
DATABASE_URL=postgresql://.../slowquery \
DATABASE_URL_FAST=postgresql://.../slowquery-fast \
  uv run python -m benchmarks.bench_demo_latency --iterations 200
```

or pass the URLs explicitly:

```bash
uv run python -m benchmarks.bench_demo_latency \
  --slow-url postgresql://.../slowquery \
  --fast-url postgresql://.../slowquery-fast \
  --iterations 200 --limit 20
```

Example output shape (numbers will differ on your environment):

```
query: SELECT id, user_id, status, total_cents, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2
iterations per branch: 200
slow  n=200   p50=  ...ms p95=  ...ms p99=  ...ms
fast  n=200   p50=  ...ms p95=  ...ms p99=  ...ms
p95 improved by ...ms switching slow -> fast
```

## The numbers are seed- and environment-dependent

Read this before quoting any figure you get:

- **Seed size matters most.** At the documented 100k-order seed the
  delta is modest -- Postgres often picks a Bitmap Heap Scan on a 100k
  table even without the ideal index, so the "before" case is already
  fairly cheap. The full 1M-order seed tips the cost calculus toward a
  sequential scan and produces a much larger, more dramatic delta. See
  [`docs/DEVIATIONS.md`](../docs/DEVIATIONS.md) sections 1 and 4.
- **The Neon compute tier and network round-trip dominate small
  queries.** On Neon Free the per-query round-trip can swamp the actual
  scan time, compressing the visible delta.
- **Warm vs cold caches matter.** The script warms the connection once
  before timing, but shared-buffer state on the server still moves the
  numbers run to run.

Because of all this, the benchmark prints what it measured and does not
hard-code an expected delta. If `fast` is not faster than `slow`, the
most likely cause is a seed too small to make the sequential scan
expensive -- scale the seed and re-run.
