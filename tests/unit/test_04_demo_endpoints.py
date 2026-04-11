"""S4: unit-lane tests for spec 04 (demo REST endpoints).

Tests here exercise API shape, error paths, cursor validation, and
grep guards. They run with a mock AsyncSession from ``conftest.py``
so no real database is required. Tests that need real data (listing,
cursor round-trip, pagination correctness) live in
``tests/integration/test_04_demo_endpoints.py``.
"""

from __future__ import annotations

import base64


def test_unknown_user_returns_404_typed_error(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 2 (partial — 404 path only, no seeded data)."""
    resp = test_client.get("/users/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"error": "user_not_found"}


def test_malformed_uuid_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 11: FastAPI's UUID validator rejects before any DB call."""
    resp = test_client.get("/users/not-a-uuid")
    assert resp.status_code == 422


def test_order_not_found_typed_error(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 12."""
    resp = test_client.get("/orders/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json() == {"error": "order_not_found"}


def test_cursor_invalid_base64_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 15."""
    resp = test_client.get("/users?cursor=@@@@not-base64@@@@")
    assert resp.status_code == 422
    assert resp.json() == {"error": "invalid_cursor"}


def test_cursor_malformed_tuple_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 16."""
    bad = base64.urlsafe_b64encode(b'{"oops": "wrong shape"}').decode()
    resp = test_client.get(f"/users?cursor={bad}")
    assert resp.status_code == 422
    assert resp.json() == {"error": "invalid_cursor"}


def test_no_free_text_filter_params(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 17."""
    schema = test_client.get("/openapi.json").json()
    users_get = schema["paths"]["/users"]["get"]
    param_names = {p["name"] for p in users_get.get("parameters", [])}
    # Only the documented params may appear.
    assert "sort" not in param_names
    assert param_names <= {"limit", "cursor"}


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
        assert 'text(f"' not in src, f"{repo.__name__} uses f-string inside text()"
        assert "f'SELECT" not in src
        assert 'f"SELECT' not in src
