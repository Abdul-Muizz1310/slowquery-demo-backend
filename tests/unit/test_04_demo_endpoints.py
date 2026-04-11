"""S3 red: unit tests for spec 04 (demo REST endpoints).

These tests use the FastAPI TestClient against a pre-seeded small dataset
(100 users, 1k orders, 5k items, 20 products) loaded in a Testcontainers
Postgres — which makes them effectively *integration* tests from the
marker perspective. They live under tests/unit/ because the plan keeps
API-shape tests in the unit lane. S4 will mark them appropriately.
"""

from __future__ import annotations


def test_list_users_returns_limited_rows_and_cursor(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 1."""
    resp = test_client.get("/users?limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 10
    assert "next_cursor" in body


def test_get_user_by_id_and_404(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 2."""
    resp = test_client.get("/users/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"error": "user_not_found"}


def test_list_user_orders_order_desc(test_client, sample_user_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 3."""
    resp = test_client.get(f"/users/{sample_user_id}/orders")
    assert resp.status_code == 200
    items = resp.json()["items"]
    created = [row["created_at"] for row in items]
    assert created == sorted(created, reverse=True)


def test_list_recent_orders(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 4."""
    resp = test_client.get("/orders?limit=5")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 5


def test_get_order_with_items(test_client, sample_order_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 5."""
    resp = test_client.get(f"/orders/{sample_order_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert isinstance(body["items"], list)


def test_list_items_by_product(test_client, sample_product_id) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 6."""
    resp = test_client.get(f"/order_items?product_id={sample_product_id}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert all(row["product_id"] == str(sample_product_id) for row in items)


def test_cursor_roundtrip(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 7."""
    first = test_client.get("/users?limit=10").json()
    second = test_client.get(f"/users?limit=10&cursor={first['next_cursor']}").json()
    first_ids = {row["id"] for row in first["items"]}
    second_ids = {row["id"] for row in second["items"]}
    assert first_ids.isdisjoint(second_ids)


def test_malformed_uuid_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 11."""
    resp = test_client.get("/users/not-a-uuid")
    assert resp.status_code == 422


def test_order_not_found_typed_error(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 12."""
    resp = test_client.get("/orders/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"error": "order_not_found"}


def test_limit_clamped_to_max_page_size(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 13."""
    resp = test_client.get("/users?limit=10000")
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 100  # MAX_PAGE_SIZE


def test_dead_pool_returns_503(test_client_dead_pool) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 14."""
    resp = test_client_dead_pool.get("/users?limit=10")
    assert resp.status_code == 503


def test_cursor_invalid_base64_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 15."""
    resp = test_client.get("/users?cursor=@@@@not-base64@@@@")
    assert resp.status_code == 422


def test_cursor_malformed_tuple_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 16."""
    import base64

    bad = base64.urlsafe_b64encode(b"{'oops': 'wrong shape'}").decode()
    resp = test_client.get(f"/users?cursor={bad}")
    assert resp.status_code == 422


def test_no_free_text_filter_params(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 17."""
    resp = test_client.get("/users?sort=malicious")
    # Unknown query parameter must not be silently accepted — FastAPI's default
    # is to ignore unknowns, so we assert the endpoint doesn't expose `sort`
    # at all by checking the OpenAPI schema.
    schema = test_client.get("/openapi.json").json()
    assert "sort" not in str(schema["paths"]["/users"])
    # And the request still works (ignored param is not an error).
    assert resp.status_code == 200


def test_repositories_use_parameterized_sql_only() -> None:
    """Spec 04 test 18: grep guard."""
    import inspect

    from slowquery_demo.repositories import (
        order_item_repository,
        order_repository,
        product_repository,
        user_repository,
    )

    for repo in (
        user_repository,
        product_repository,
        order_repository,
        order_item_repository,
    ):
        src = inspect.getsource(repo)
        # text("SELECT ...") with an f-string is the dangerous pattern.
        assert 'text(f"' not in src, f"{repo.__name__} uses f-string inside text()"
        assert "f'SELECT" not in src
        assert 'f"SELECT' not in src
