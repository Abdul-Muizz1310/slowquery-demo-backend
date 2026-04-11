# Spec 00 — Database schema

## Goal

Define the SQLAlchemy async ORM base and eight tables that back the demo service: four for the commerce dataset the traffic generator hammers (`users`, `products`, `orders`, `order_items`) and four bookkeeping tables written by `slowquery-detective`'s `StoreWriter` (`query_fingerprints`, `query_samples`, `explain_plans`, `suggestions`). Ship one Alembic migration (`0001_initial.py`) that creates the full schema against whichever Neon branch `DATABASE_URL` points at. The migration is deliberately **index-poor on the commerce tables** on the `slowquery` branch — the missing indexes on `orders.user_id`, `order_items.order_id`, and `order_items.product_id` are the point of the demo. The `slowquery-fast` branch gets the same schema plus those three indexes added out-of-band by `scripts/seed_fast.py` (see Spec 03).

## Inputs / Outputs

- **In:** `DATABASE_URL` (env var, `postgresql+asyncpg://...`), `alembic upgrade head`.
- **Out:** eight tables present, `alembic_version` row at `0001`, connection still usable by the demo service.
- **Shape of the ORM layer:** `slowquery_demo.models.Base = DeclarativeBase` (async-compatible via SQLAlchemy 2.0 async mappings), exposed as `Base.metadata` to `alembic/env.py`.

## Table-by-table surface

### Commerce tables (indexes deliberately absent on `slowquery`)

| Table | Columns | Notes |
|---|---|---|
| `users` | `id UUID PK`, `email TEXT UNIQUE NOT NULL`, `full_name TEXT NOT NULL`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` | unique index on email (keeps `/users` lookups fast — the point is not to make *everything* slow) |
| `products` | `id UUID PK`, `sku TEXT UNIQUE NOT NULL`, `name TEXT NOT NULL`, `price_cents BIGINT NOT NULL CHECK > 0`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` | unique index on sku |
| `orders` | `id UUID PK`, `user_id UUID NOT NULL REFERENCES users(id)`, `status order_status NOT NULL DEFAULT 'pending'`, `total_cents BIGINT NOT NULL CHECK >= 0`, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` | **no index on `user_id`** — this is the slowness we want to detect |
| `order_items` | `id UUID PK`, `order_id UUID NOT NULL REFERENCES orders(id)`, `product_id UUID NOT NULL REFERENCES products(id)`, `quantity INT NOT NULL CHECK > 0`, `unit_price_cents BIGINT NOT NULL CHECK >= 0` | **no index on `order_id` or `product_id`** — the rules engine should light up on both |

`order_status` is a Postgres `ENUM` with members `{'pending','paid','shipped','cancelled'}`. All FKs are `ON DELETE CASCADE` so seeds can be re-run by truncating in the right order.

### Bookkeeping tables (written by slowquery-detective `StoreWriter`)

Exactly match the data model in [`docs/projects/50-slowquery-detective.md`](../../../../docs/projects/50-slowquery-detective.md) §"Data model" — any drift from that spec is a bug in this spec, not a design choice:

| Table | Purpose |
|---|---|
| `query_fingerprints` | one row per fingerprint; rolling p50/p95/p99/max, total_ms, call_count |
| `query_samples` | one row per captured slow query; joined on fingerprint_id |
| `explain_plans` | one row per fingerprint (PK on `fingerprint_id`); plan_json + plan_text + cost |
| `suggestions` | one row per (fingerprint, kind, source); rationale + optional DDL |

Indexes on the bookkeeping tables: `query_samples (fingerprint_id, sampled_at DESC)`, `suggestions (fingerprint_id)`. These are fast tables by design — they're the admin surface the dashboard reads.

## Invariants

1. Every FK on a commerce table uses `ON DELETE CASCADE` so seeds are idempotent with truncate.
2. `CHECK` constraints make invalid money / quantities unrepresentable at the DB layer (negative prices, zero quantities).
3. `order_status` is a typed enum, not a free-text column. Adding a value requires a migration.
4. The `slowquery` branch must **not** have indexes on `orders.user_id`, `order_items.order_id`, or `order_items.product_id` after `alembic upgrade head`. The rules engine demo depends on their absence.
5. `alembic upgrade head` on an empty database creates all eight tables. `alembic downgrade base` drops them cleanly (including the enum type).
6. `target_metadata` in `alembic/env.py` is wired to `Base.metadata` and `alembic revision --autogenerate` produces an empty diff against a freshly-upgraded database.

## Test cases

**Success (unit — pure ORM inspection, no DB):**
1. `Base.metadata.tables` contains exactly the eight expected table names.
2. `orders` has an FK on `user_id` referencing `users(id)` with `ondelete='CASCADE'`.
3. `order_items` has two FKs (`order_id`, `product_id`) with `ondelete='CASCADE'`.
4. `order_status` enum is defined with exactly the four expected members.
5. Check constraints on `products.price_cents`, `orders.total_cents`, `order_items.quantity`, `order_items.unit_price_cents` exist and carry the expected predicates.
6. `query_fingerprints.id` is a string PK ≤16 chars (matches the library's `fingerprint()` output).
7. `explain_plans.fingerprint_id` is the PK (one plan per fingerprint).

**Success (integration — Testcontainers Postgres, marked `@pytest.mark.integration`):**
8. `alembic upgrade head` against an empty db creates every table, ends on revision `0001`.
9. `alembic downgrade base` drops every table + the `order_status` enum. No orphan types left behind.
10. `alembic revision --autogenerate` against an already-upgraded db produces an empty diff (proof `target_metadata` matches reality).
11. Inserting a row into `orders` with an invalid status (`'expired'`) raises a `DataError`.
12. Deleting a `users` row cascades to its `orders` and (transitively) `order_items`.
13. Inserting `products` with `price_cents = 0` is rejected by the CHECK constraint.

**Failure / negative:**
14. `alembic upgrade head` with `DATABASE_URL` unset raises `RuntimeError("DATABASE_URL environment variable is required …")`.
15. Dropping `order_status` while rows reference it raises (the downgrade path must drop tables first, then the type).

**Security / destructive-guard:**
16. There is **no** migration step that creates indexes on `orders.user_id`, `order_items.order_id`, or `order_items.product_id`. A dedicated test greps the migration file for `CREATE INDEX` on those columns and fails if any appear — protects the demo from a well-meaning future refactor silently breaking the slow-path story.

## Acceptance

- [ ] `src/slowquery_demo/models/base.py` defines `Base = DeclarativeBase`.
- [ ] `src/slowquery_demo/models/{user,product,order,order_item}.py` declare the four commerce models.
- [ ] `src/slowquery_demo/models/slowquery_store.py` declares the four bookkeeping models (kept in a separate file so the commerce MVC layer doesn't import slowquery internals).
- [ ] `alembic/versions/0001_initial.py` is written by hand (not autogenerated — the exact DDL is part of the spec).
- [ ] `alembic/env.py` `target_metadata = Base.metadata`.
- [ ] All 16 test cases above have a corresponding test (unit + integration).
- [ ] Test 16 (the "no-index guard") must remain green for the life of the repo.
