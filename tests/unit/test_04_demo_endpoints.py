"""S4: unit-lane tests for spec 04 (demo REST endpoints).

Tests here exercise API shape, error paths, cursor validation, and
grep guards. They run with a mock AsyncSession from ``conftest.py``
so no real database is required. Tests that need real data (listing,
cursor round-trip, pagination correctness) live in
``tests/integration/test_04_demo_endpoints.py``.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slowquery_demo.core.errors import OrderNotFoundError, ProductNotFoundError


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


def test_limit_below_range_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04: limit=0 violates the ge=1 boundary and 422s at the API edge."""
    assert test_client.get("/orders?limit=0").status_code == 422


def test_limit_above_range_returns_422(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04: limit>100 violates the le=100 boundary and 422s at the API edge."""
    assert test_client.get("/products?limit=1000").status_code == 422


def test_limit_within_range_ok(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04: an in-range limit passes validation."""
    assert test_client.get("/orders?limit=50").status_code == 200


def test_limit_documented_range_in_openapi(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04: /docs now documents the real 1..100 contract for limit."""
    schema = test_client.get("/openapi.json").json()
    params = schema["paths"]["/orders"]["get"]["parameters"]
    limit = next(p for p in params if p["name"] == "limit")
    # ``int | None`` renders as an anyOf; the integer branch carries the bounds.
    serialized = str(limit["schema"])
    assert "'minimum': 1" in serialized
    assert "'maximum': 100" in serialized


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


# ---------------------------------------------------------------------------
# Pagination schema (spec 04): limit clamping is negative-space; the boundary
# type allows any int but the schema must clamp to [1, MAX] and default on
# None/0/negative. Cursor encode/decode must round-trip losslessly.
# ---------------------------------------------------------------------------


def test_clamp_limit_defaults_on_none_zero_and_negative() -> None:
    """Spec 04: clamp_limit coerces None/0/negative to the default page size."""
    from slowquery_demo.schemas.pagination import DEFAULT_PAGE_SIZE, clamp_limit

    assert clamp_limit(None) == DEFAULT_PAGE_SIZE
    assert clamp_limit(0) == DEFAULT_PAGE_SIZE
    assert clamp_limit(-5) == DEFAULT_PAGE_SIZE


def test_clamp_limit_passes_in_range_and_caps_above_max() -> None:
    """Spec 04: an in-range value passes; anything above MAX is capped to MAX."""
    from slowquery_demo.schemas.pagination import MAX_PAGE_SIZE, clamp_limit

    assert clamp_limit(50) == 50
    assert clamp_limit(1000) == MAX_PAGE_SIZE


def test_cursor_encode_decode_roundtrip() -> None:
    """Spec 04 test 15/16 (positive): a well-formed cursor round-trips."""
    from slowquery_demo.schemas.pagination import decode_cursor, encode_cursor

    encoded = encode_cursor("2025-01-01T00:00:00Z", "abc-123")
    decoded = decode_cursor(encoded)
    assert decoded.created_at == "2025-01-01T00:00:00Z"
    assert decoded.id == "abc-123"


# ---------------------------------------------------------------------------
# Repository layer (spec 04): repos are the only SQLAlchemy importers. These
# exercise each read against a mock AsyncSession so the query wiring (execute
# → scalar_one_or_none / scalars().all()) is covered without a real DB; the
# SQL correctness itself is asserted against real Postgres in the integration
# lane.
# ---------------------------------------------------------------------------


def _mock_session_scalar_one_or_none(value: object) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()  # result is not awaited
    result.scalar_one_or_none.return_value = value
    session.execute.return_value = result
    return session


def _mock_session_scalars_all(values: list[object]) -> AsyncMock:
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars_mock
    session.execute.return_value = result
    return session


async def test_order_repository_reads() -> None:
    """Spec 04: order_repository get_by_id / list_recent / list_for_user."""
    from slowquery_demo.repositories import order_repository

    mock_order = MagicMock()
    assert (
        await order_repository.get_by_id(_mock_session_scalar_one_or_none(mock_order), uuid.uuid4())
        is mock_order
    )
    assert await order_repository.list_recent(
        _mock_session_scalars_all([mock_order]), limit=10
    ) == [mock_order]
    assert await order_repository.list_for_user(
        _mock_session_scalars_all([mock_order]), uuid.uuid4(), limit=10
    ) == [mock_order]


async def test_product_repository_reads() -> None:
    """Spec 04: product_repository get_by_id / list_products."""
    from slowquery_demo.repositories import product_repository

    mock_product = MagicMock()
    assert (
        await product_repository.get_by_id(
            _mock_session_scalar_one_or_none(mock_product), uuid.uuid4()
        )
        is mock_product
    )
    assert await product_repository.list_products(
        _mock_session_scalars_all([mock_product]), limit=10
    ) == [mock_product]


async def test_order_item_repository_reads() -> None:
    """Spec 04: order_item_repository list_for_order / list_for_product."""
    from slowquery_demo.repositories import order_item_repository

    mock_item = MagicMock()
    assert await order_item_repository.list_for_order(
        _mock_session_scalars_all([mock_item]), uuid.uuid4()
    ) == [mock_item]
    assert await order_item_repository.list_for_product(
        _mock_session_scalars_all([mock_item]), uuid.uuid4(), limit=10
    ) == [mock_item]


# ---------------------------------------------------------------------------
# Service layer (spec 04): services turn ORM rows into frozen DTOs and raise
# typed domain errors on a missing row (negative-space: a None from the repo
# must become a typed 404, never an AttributeError deeper in the stack).
# ---------------------------------------------------------------------------


async def test_get_order_with_items_raises_typed_error_when_missing() -> None:
    """Spec 04 test 12 (service level): missing order → OrderNotFoundError."""
    from slowquery_demo.services import order_service

    with patch.object(order_service, "order_repository") as repo:
        repo.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(OrderNotFoundError):
            await order_service.get_order_with_items(AsyncMock(), uuid.uuid4())


async def test_get_order_with_items_maps_row_to_dto() -> None:
    """Spec 04: happy path assembles the order + its items into a DTO."""
    from slowquery_demo.services import order_service

    order_id = uuid.uuid4()
    mock_order = MagicMock(
        id=order_id,
        user_id=uuid.uuid4(),
        status="pending",
        total_cents=1000,
        created_at="2025-01-01T00:00:00Z",
    )
    mock_item = MagicMock(
        id=uuid.uuid4(),
        order_id=order_id,
        product_id=uuid.uuid4(),
        quantity=2,
        unit_price_cents=500,
        created_at="2025-01-01T00:00:00Z",
    )
    with (
        patch.object(order_service, "order_repository") as order_repo,
        patch.object(order_service, "order_item_repository") as item_repo,
    ):
        order_repo.get_by_id = AsyncMock(return_value=mock_order)
        item_repo.list_for_order = AsyncMock(return_value=[mock_item])
        result = await order_service.get_order_with_items(AsyncMock(), order_id)

    assert result.id == order_id
    assert len(result.items) == 1


async def test_order_service_list_helpers_wrap_rows_in_paginated_response() -> None:
    """Spec 04: list_recent_orders / list_user_orders / list_items_for_product
    each return a PaginatedResponse over the repo rows."""
    from slowquery_demo.services import order_service

    row = MagicMock(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        order_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        status="shipped",
        total_cents=2000,
        quantity=1,
        unit_price_cents=300,
        created_at="2025-01-02T00:00:00Z",
    )
    with patch.object(order_service, "order_repository") as order_repo:
        order_repo.list_recent = AsyncMock(return_value=[row])
        order_repo.list_for_user = AsyncMock(return_value=[row])
        assert len((await order_service.list_recent_orders(AsyncMock(), limit=10)).items) == 1
        assert (
            len((await order_service.list_user_orders(AsyncMock(), uuid.uuid4(), limit=10)).items)
            == 1
        )
    with patch.object(order_service, "order_item_repository") as item_repo:
        item_repo.list_for_product = AsyncMock(return_value=[row])
        assert (
            len(
                (
                    await order_service.list_items_for_product(AsyncMock(), uuid.uuid4(), limit=10)
                ).items
            )
            == 1
        )


async def test_get_product_raises_typed_error_when_missing() -> None:
    """Spec 04: missing product → ProductNotFoundError."""
    from slowquery_demo.services import product_service

    with patch.object(product_service, "product_repository") as repo:
        repo.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(ProductNotFoundError):
            await product_service.get_product(AsyncMock(), uuid.uuid4())


async def test_product_service_maps_rows() -> None:
    """Spec 04: get_product / list_products map ORM rows to DTOs."""
    from slowquery_demo.services import product_service

    pid = uuid.uuid4()
    # ``name`` is a reserved MagicMock kwarg — set it as an attribute instead.
    row = MagicMock(id=pid, sku="WIDGET-001", price_cents=999, created_at="2025-01-01T00:00:00Z")
    row.name = "Widget"
    with patch.object(product_service, "product_repository") as repo:
        repo.get_by_id = AsyncMock(return_value=row)
        repo.list_products = AsyncMock(return_value=[row])
        assert (await product_service.get_product(AsyncMock(), pid)).id == pid
        assert len((await product_service.list_products(AsyncMock(), limit=10)).items) == 1


# ---------------------------------------------------------------------------
# Router layer (spec 04): the list endpoints wire router → service → repo end
# to end. With the empty mock session from the fixture they must return 200
# with an empty page (not 500), proving the happy-path return is reached.
# ---------------------------------------------------------------------------


def test_list_endpoints_return_200_empty_page(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 04: /products, /orders, /order_items list handlers return 200."""
    assert test_client.get("/products").status_code == 200
    assert test_client.get("/orders").status_code == 200
    assert test_client.get(f"/order_items?product_id={uuid.uuid4()}").status_code == 200
