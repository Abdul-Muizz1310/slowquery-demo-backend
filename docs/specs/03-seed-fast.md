# Spec 03 — `scripts/seed_fast.py`

## Goal

Populate the `slowquery-fast` Neon branch with the **same rows** as the `slowquery` branch from Spec 02, then create the three indexes the slow branch deliberately omits. The two branches must be row-identical so that the `POST /branches/switch` endpoint (Spec 06) produces a visible p95 drop *only* because of indexing — never because the datasets differ. Without that guarantee, the demo's 1200ms → 18ms story is an artifact of two different datasets, and the whole project loses its credibility.

## Inputs / Outputs

- **In:** `DATABASE_URL_FAST` (env, points at the `slowquery-fast` branch), and `--seed` (defaults to `42`, must match the seed used on the slow branch).
- **Out:** the same row counts as `seed_slow` plus three indexes:
  - `CREATE INDEX ix_orders_user_id ON orders(user_id);`
  - `CREATE INDEX ix_order_items_order_id ON order_items(order_id);`
  - `CREATE INDEX ix_order_items_product_id ON order_items(product_id);`
- Exit code `0` on success. Flags mirror `seed_slow.py`: `--reset`, `--users`, `--orders`, `--order-items`, `--products`, `--seed`.

## Invariants

1. `seed_fast.py` delegates row generation to the same pure helpers (`build_user_rows`, `build_order_rows`, etc.) from `seed_slow.py` so the two scripts **cannot drift**. Row generation logic lives in a shared module (`scripts/_seed_common.py`) that both scripts import.
2. The script uses `DATABASE_URL_FAST`, never `DATABASE_URL`. A test asserts this by injecting both env vars and confirming the script opens a connection to `DATABASE_URL_FAST`.
3. The three `CREATE INDEX` calls are the **last** statements the script runs. Indexes are created after all rows are inserted because creating indexes up-front and then bulk-inserting is dramatically slower on Postgres.
4. The script is idempotent under `--reset` AND the indexes are `CREATE INDEX IF NOT EXISTS` so re-running without `--reset` after a previous successful run is a no-op (not an error).
5. Row counts on the fast branch match row counts on the slow branch under equivalent `--users/--orders/--order-items/--products/--seed` flags. A property-based test hashes the rows (`(email, full_name)` for users; `(user_id, total_cents, created_at)` for orders, etc.) on both branches and asserts set equality.

## Test cases

**Success (unit — shared helpers imported from `_seed_common`):**
1. `seed_fast.py` imports `build_user_rows`, `build_order_rows`, `build_order_item_rows`, `build_product_rows` from `_seed_common`. Grep test: it does not redefine them.
2. `parse_args(["--users", "100"])` matches `seed_slow`'s parser byte-for-byte (same dataclass, same defaults except the env-var name).
3. The three indexed columns are stored in a module-level `FAST_INDEXES: Final[tuple[str, ...]]` constant and the script emits them in that order.

**Success (integration — two Testcontainers databases, `@pytest.mark.integration`):**
4. After running both seeds with matching `--seed 42 --users 100 --orders 1000 --order-items 5000 --products 20`, the set of `(email, full_name, created_at_microsecond)` tuples is identical on both branches.
5. On the `slowquery-fast` container, `SELECT indexname FROM pg_indexes WHERE tablename = 'orders'` includes `ix_orders_user_id`; same for the two `order_items` indexes.
6. On the `slowquery` container from Spec 02, none of those three indexes exist.
7. Running `EXPLAIN (FORMAT JSON) SELECT * FROM orders WHERE user_id = <any existing uuid>` on the fast container returns a plan with `"Node Type": "Index Scan"` — proof the indexes are being used.
8. The same query on the slow container returns a plan with `"Node Type": "Seq Scan"` and a row count close to the total `orders` count — proof the rules engine will fire.
9. Running `seed_fast.py --reset` twice in a row ends up with the same row + index state both times.
10. Running `seed_fast.py` without `--reset` after a successful run exits `0` and is a no-op (does not re-index, does not insert).

**Failure / negative:**
11. Running with `DATABASE_URL_FAST` unset exits `1` with a clear error.
12. Running with `DATABASE_URL_FAST` pointing at a database that hasn't been migrated exits `1` with a clear error.
13. Running with a `--seed` value that differs from the value used on the slow branch still succeeds — the script has no way to know — but a separate invariant test (Spec 06) exercises the end-to-end "switch doesn't create row drift" contract and catches this.

**Security / destructive-guard:**
14. `seed_fast.py` refuses to run if `DATABASE_URL_FAST` does not contain `slowquery-fast` or a loopback address.
15. The indexes created are exhaustively listed in `FAST_INDEXES`; the test asserts that no `CREATE INDEX` string appears outside that constant (grep).
16. The script does not accept arbitrary DDL via flags. No `--extra-index` or similar escape hatch.

## Acceptance

- [ ] `scripts/_seed_common.py` hosts all row-generation helpers, shared by both seed scripts.
- [ ] `scripts/seed_fast.py` is thin glue: parse args, reset (optional), insert rows, create indexes.
- [ ] `FAST_INDEXES: Final[tuple[str, ...]]` enumerates the three CREATE INDEX statements.
- [ ] Row-identity between slow and fast branches is enforced by at least one integration test and is the primary invariant of the file.
- [ ] Every test case above has a corresponding test.
