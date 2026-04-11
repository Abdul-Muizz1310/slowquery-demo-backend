# Spec 02 — `scripts/seed_slow.py`

## Goal

Populate the `slowquery` Neon branch with the dataset the live demo hammers: 10,000 users, 1,000,000 orders, 5,000,000 order_items, and ~2,000 products. The defining feature of this seed is what it **doesn't** do — it leaves `orders.user_id`, `order_items.order_id`, and `order_items.product_id` un-indexed. Those three missing indexes are the reason the rules engine has anything to say, and the reason the live dashboard shows a p95 over a second on the slow branch.

## Inputs / Outputs

- **In:** `DATABASE_URL` (env, points at the `slowquery` branch), optional CLI flags:
  - `--reset` — `TRUNCATE order_items, orders, products, users RESTART IDENTITY CASCADE` before inserting.
  - `--users N` / `--orders N` / `--order-items N` / `--products N` — override defaults for small-dataset smoke runs in CI.
  - `--seed INT` — RNG seed for reproducible datasets (defaults to `42`).
- **Out:** stdout progress log (structlog, one line per batch), exit code `0` on success.
- **Schema assumption:** `alembic upgrade head` has already been run. The script does **not** run migrations — it's a data-only seed.

## Invariants

1. The script uses Postgres `COPY FROM STDIN` via `asyncpg.Connection.copy_records_to_table()` for bulk inserts. Row-at-a-time INSERTs would take hours on 5M order_items; COPY makes it tractable on a Neon free-tier compute.
2. Referential integrity holds: every `orders.user_id` exists in `users.id`, every `order_items.order_id` exists in `orders.id`, every `order_items.product_id` exists in `products.id`. The script builds the parent id arrays in memory first and samples from them, rather than hoping for the best.
3. The distribution is **skewed on purpose** — a power-law distribution over users-per-order and orders-per-user so that a small minority of users have thousands of orders. This makes the seq-scan cost on the slow branch visible instead of negligible.
4. **No `CREATE INDEX` statement anywhere in the script**, ever, for any of the three demo-critical columns. A grep-based self-test on the source file is part of the test suite (same guard as Spec 00 test 16, but caught one level earlier).
5. The script is idempotent under `--reset`: running it twice with `--reset` leaves the same row counts. Without `--reset` it refuses to run against a non-empty `users` table (exit code `1`) so nobody accidentally doubles the data.
6. Progress logging is structlog-based, JSON in prod, pretty in dev. Every 100k-row batch logs `{"event": "seed.progress", "table": "order_items", "written": 500000, "total": 5000000}`.

## Test cases

**Success (unit — pure helpers, no DB):**
1. `build_user_rows(n=100, seed=42)` returns 100 rows with unique emails (`user_{i}@example.com`) and deterministic `full_name`s.
2. `build_order_rows(user_ids, n=1000, seed=42)` returns 1000 rows whose `user_id` values are all drawn from `user_ids` and whose `total_cents` distribution has a long right tail (Zipfian-like).
3. `build_order_item_rows(order_ids, product_ids, n=5000, seed=42)` never emits an `order_id` or `product_id` not in the supplied arrays.
4. The row builders are pure: same seed → same output, verified by hashing the output.
5. `parse_args(["--users", "100"])` returns the documented dataclass with the override applied.

**Success (integration — Testcontainers Postgres + real schema, `@pytest.mark.integration`):**
6. Running `seed_slow --reset --users 100 --orders 1000 --order-items 5000 --products 20` on a freshly-migrated database leaves exactly those row counts.
7. `SELECT COUNT(*) FROM pg_indexes WHERE tablename = 'orders' AND indexdef LIKE '%user_id%'` returns `0` after the seed finishes. Same for `order_items.order_id` and `order_items.product_id`. This is the whole point.
8. A re-run without `--reset` exits `1` and writes nothing.
9. A second `--reset` run restores the exact same row counts (idempotent).
10. Joining `order_items` to `orders` to `users` for a sample user returns a plausible per-user order history (FK integrity holds end-to-end).

**Failure / negative:**
11. Running with `DATABASE_URL` unset exits `1` with a clear error ("DATABASE_URL is required").
12. Running against a database that has not been migrated (no `users` table) exits `1` with a clear error suggesting `alembic upgrade head`.
13. `--users 0` is rejected by argparse / Pydantic with a clear error (row counts must be positive).
14. A `KeyboardInterrupt` mid-seed leaves the database in a consistent state: all copies run inside a single transaction so an interrupt rolls everything back.

**Security / destructive-guard:**
15. The grep self-test: `CREATE INDEX` does not appear anywhere in the script body. If a future change adds one on any of the three forbidden columns, the test fails.
16. The script refuses to run if `DATABASE_URL` appears to point at a production database (simple allowlist check: the URL must contain `slowquery` or `localhost` or `127.0.0.1`). This is belt-and-suspenders — the real protection is that prod credentials aren't in the demo service's env.

## Acceptance

- [ ] `scripts/seed_slow.py` is an `if __name__ == "__main__": asyncio.run(main())` script, not a Python module.
- [ ] Depends only on `asyncpg`, `typer` (or `argparse`), and stdlib — no ORM round-trips for the bulk path.
- [ ] Default run produces 10k / 1M / 5M / 2k rows in under 10 minutes on a Neon free-tier compute (benchmarked in integration test with smaller N and extrapolated).
- [ ] Every test case above has a corresponding test.
- [ ] The grep self-test lives in `tests/unit/test_seed_slow_guards.py` and is free of any import dependencies on the script's runtime modules (so it runs fast in CI without asyncpg installed).
