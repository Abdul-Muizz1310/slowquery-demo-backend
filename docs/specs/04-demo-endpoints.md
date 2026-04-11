# Spec 04 — Demo REST endpoints

## Goal

Replace the feathers-generated placeholder routers (which return `{"handler": "users.list"}`) with real, async-SQLAlchemy-backed endpoints that exercise the seeded dataset. These endpoints are not the product — `slowquery-detective` is the product — but they're load-bearing: the traffic generator in Spec 07 hits them to produce the sequential-scan patterns that light up the rules engine on the slow branch. Every endpoint is designed to **intentionally issue one of the known-bad query shapes** on the slow branch.

## Inputs / Outputs

Four router files under `src/slowquery_demo/api/routers/`, one per commerce model, calling into `services/<model>_service.py` → `repositories/<model>_repository.py`. MVC layering is strict; controllers never touch `AsyncSession` directly.

### Endpoints (matching the generated scaffold)

| Method | Path | Rule targeted | Service call |
|---|---|---|---|
| `GET` | `/users` | — (fast path, unique index on email) | `list_users(limit, cursor)` |
| `GET` | `/users/{id}` | — (PK lookup) | `get_user(id)` |
| `GET` | `/users/{id}/orders` | **Seq scan on `orders.user_id`** | `list_user_orders(user_id, limit)` |
| `GET` | `/products` | — | `list_products(limit, cursor)` |
| `GET` | `/orders` | `ORDER BY created_at DESC` without composite index | `list_recent_orders(limit)` |
| `GET` | `/orders/{id}` | PK lookup + join to `order_items` (**seq scan on `order_items.order_id`**) | `get_order_with_items(id)` |
| `GET` | `/order_items` | `WHERE product_id = ?` (**seq scan on `order_items.product_id`**) | `list_items_for_product(product_id, limit)` |

## Invariants

1. Every endpoint returns a Pydantic v2 DTO defined in `src/slowquery_demo/schemas/`. No raw ORM objects cross the route boundary.
2. Cursor pagination uses opaque base64-encoded `(created_at, id)` tuples, not offset/limit. Keeps the API honest about large result sets.
3. Services are pure-ish: they accept an `AsyncSession` from FastAPI's dependency system but contain no HTTP concepts. Repositories are the only layer that writes SQL.
4. Errors are typed: `UserNotFoundError`, `OrderNotFoundError`, `ProductNotFoundError` bubble out of services and get mapped to 404s by an `exception_handler` in `main.py`. No bare `HTTPException` raised from inside services.
5. Every service method that issues a "slow" query pattern has a comment (`# slow-path: seq scan on <column>`) pointing at the rule it's designed to trigger. This is for humans reading the code, not for the rules engine.
6. The endpoints do not bypass slowquery-detective. They run through the middleware the same way a user's traffic would, so the fingerprints, samples, plans, and suggestions are all populated by real calls — not synthesized.

## Test cases

**Success (unit — pure service + repo tests against Testcontainers Postgres with 100-row seed):**
1. `GET /users?limit=10` returns 10 users in a deterministic order. `next_cursor` round-trips.
2. `GET /users/{id}` returns the expected user; unknown id returns 404 with body `{"error": "user_not_found"}`.
3. `GET /users/{id}/orders` returns the orders for that user, ordered by `created_at DESC`. Empty list for a user with no orders.
4. `GET /orders?limit=5` returns the 5 most recent orders across all users.
5. `GET /orders/{id}` returns the order plus a nested `items: [...]` list.
6. `GET /order_items?product_id=...` returns all items referencing that product.
7. Pagination: fetching with `cursor` from response `n` returns the next page starting exactly where the previous one stopped.

**Success (integration — `@pytest.mark.integration` against a small seeded db):**
8. Hitting `GET /users/{id}/orders` through a `TestClient` with the slowquery-detective middleware attached results in exactly one fingerprint being recorded in `query_fingerprints` matching the parameterized SQL.
9. On a seeded db large enough that `EXPLAIN` returns a seq scan (e.g. 10k orders), hitting `GET /users/{id}/orders` once triggers the `seq_scan_large_table` rule — a suggestion row appears in the `suggestions` table within 5 seconds.
10. On the `slowquery-fast` DB (3 indexes present), the same request does **not** trigger the rule — proof the endpoint's slowness is a function of schema, not query.

**Failure / negative:**
11. `GET /users/{id}` with a malformed UUID returns 422 (FastAPI validation) not 500.
12. `GET /orders/{id}` for a non-existent id returns 404 with the typed error body.
13. `GET /users?limit=10000` is capped at `MAX_PAGE_SIZE=100` and returns 100 rows.
14. A service call that hits a dropped database (pool dead) returns 503, not 500, and logs at `error` severity with the request id in structlog context.

**Security / destructive-guard:**
15. `cursor` values that fail to base64-decode return 422, never 500.
16. `cursor` values that decode to malformed tuples are rejected before touching SQL.
17. No endpoint accepts a free-text `sort` or `filter` parameter that could be used to trigger arbitrary queries. Any pagination / filtering is done via explicit, typed query params.
18. The four repositories use `sqlalchemy.select()` and `bindparam`-style params exclusively — a grep test asserts no raw `text("... f-string ...")` is used.

## Acceptance

- [ ] `src/slowquery_demo/schemas/{user,product,order,order_item}.py` hold the Pydantic DTOs.
- [ ] `src/slowquery_demo/services/{user,product,order,order_item}_service.py` hold the business logic.
- [ ] `src/slowquery_demo/repositories/{user,product,order,order_item}_repository.py` hold the SQL.
- [ ] `src/slowquery_demo/api/routers/*.py` are thin — no SQL, no ORM imports.
- [ ] `src/slowquery_demo/core/errors.py` defines the typed domain errors + `exception_handler` plumbing.
- [ ] `src/slowquery_demo/core/database.py` holds the `AsyncSession` dependency + `get_db()` factory.
- [ ] Every test case above has a corresponding test.
- [ ] No endpoint issues an N+1 pattern (the N+1 rule is fired intentionally by the traffic generator, not by the code itself — see Spec 07).
