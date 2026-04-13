"""Tests for the remaining uncovered router handler lines.

These exercise FastAPI endpoints with a mocked DB session dependency.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from slowquery_demo.core.database import get_db
from slowquery_demo.main import app
from slowquery_demo.schemas.order import OrderDTO
from slowquery_demo.schemas.pagination import PaginatedResponse
from slowquery_demo.schemas.product import ProductDTO


@pytest.fixture(autouse=True)
def _override_db():
    """Override the DB dependency with a no-op session."""
    mock_session = AsyncMock()

    async def _mock_get_db():
        yield mock_session

    app.dependency_overrides[get_db] = _mock_get_db
    yield mock_session
    app.dependency_overrides.clear()


@pytest.fixture()
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ── products.py:24 — list_products return ────────────────────────────────────


async def test_list_products_endpoint(client) -> None:
    """Cover products.py:24."""
    fake_result = MagicMock()
    fake_result.items = []
    fake_result.next_cursor = None
    fake_result.model_dump = MagicMock(return_value={"items": [], "next_cursor": None})

    with patch(
        "slowquery_demo.api.routers.products.product_service.list_products",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.get("/products")
    assert resp.status_code == 200


# ── products.py:29 — get_product return ──────────────────────────────────────


async def test_get_product_endpoint(client) -> None:
    """Cover products.py:29."""
    pid = uuid.uuid4()
    fake_dto = ProductDTO(
        id=pid,
        sku="SKU-1",
        name="Widget",
        price_cents=999,
        created_at=datetime.now(UTC),
    )

    with patch(
        "slowquery_demo.api.routers.products.product_service.get_product",
        new=AsyncMock(return_value=fake_dto),
    ):
        resp = await client.get(f"/products/{pid}")
    assert resp.status_code == 200


# ── orders.py:26 — list_recent_orders return ─────────────────────────────────


async def test_list_orders_endpoint(client) -> None:
    """Cover orders.py:26."""
    fake_result = PaginatedResponse[OrderDTO](items=[], next_cursor=None)

    with patch(
        "slowquery_demo.api.routers.orders.order_service.list_recent_orders",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.get("/orders")
    assert resp.status_code == 200


# ── order_items.py:29 — list_items_for_product return ────────────────────────


async def test_list_order_items_endpoint(client) -> None:
    """Cover order_items.py:29."""
    pid = uuid.uuid4()
    fake_result = PaginatedResponse[OrderDTO](items=[], next_cursor=None)

    with patch(
        "slowquery_demo.api.routers.order_items.order_service.list_items_for_product",
        new=AsyncMock(return_value=fake_result),
    ):
        resp = await client.get(f"/order_items?product_id={pid}")
    assert resp.status_code == 200


# ── branches.py:18 — _get_switcher success return ───────────────────────────


async def test_branches_switcher_missing_503(client) -> None:
    """Cover branches.py:18 — switcher is None → 503."""
    # Ensure no switcher is set
    if hasattr(app.state, "branch_switcher"):
        del app.state.branch_switcher
    resp = await client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 503


async def test_branches_switcher_wired(client) -> None:
    """Cover branches.py:18-19 — switcher exists, returns it."""
    now = datetime.now(UTC)
    mock_switcher = MagicMock()
    mock_switcher.switch = AsyncMock(return_value=(now, 42))
    mock_switcher.active = "fast"
    app.state.branch_switcher = mock_switcher

    resp = await client.post("/branches/switch", json={"target": "fast"})
    assert resp.status_code == 200

    del app.state.branch_switcher
