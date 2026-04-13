"""Unit tests for service layer coverage.

Exercises order_service and product_service using mock sessions.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slowquery_demo.core.errors import OrderNotFoundError, ProductNotFoundError


# ---------------------------------------------------------------------------
# order_service
# ---------------------------------------------------------------------------


async def test_get_order_with_items_not_found() -> None:
    from slowquery_demo.services import order_service

    session = AsyncMock()
    with patch.object(order_service, "order_repository") as mock_repo:
        mock_repo.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(OrderNotFoundError):
            await order_service.get_order_with_items(session, uuid.uuid4())


async def test_get_order_with_items_happy_path() -> None:
    from slowquery_demo.services import order_service

    session = AsyncMock()
    order_id = uuid.uuid4()
    user_id = uuid.uuid4()

    mock_order = MagicMock()
    mock_order.id = order_id
    mock_order.user_id = user_id
    mock_order.status = "pending"
    mock_order.total_cents = 1000
    mock_order.created_at = "2025-01-01T00:00:00Z"

    mock_item = MagicMock()
    mock_item.id = uuid.uuid4()
    mock_item.order_id = order_id
    mock_item.product_id = uuid.uuid4()
    mock_item.quantity = 2
    mock_item.unit_price_cents = 500
    mock_item.created_at = "2025-01-01T00:00:00Z"

    with (
        patch.object(order_service, "order_repository") as mock_order_repo,
        patch.object(order_service, "order_item_repository") as mock_item_repo,
    ):
        mock_order_repo.get_by_id = AsyncMock(return_value=mock_order)
        mock_item_repo.list_for_order = AsyncMock(return_value=[mock_item])

        result = await order_service.get_order_with_items(session, order_id)

    assert result.id == order_id
    assert len(result.items) == 1


async def test_list_recent_orders() -> None:
    from slowquery_demo.services import order_service

    session = AsyncMock()
    mock_order = MagicMock()
    mock_order.id = uuid.uuid4()
    mock_order.user_id = uuid.uuid4()
    mock_order.status = "pending"
    mock_order.total_cents = 1000
    mock_order.created_at = "2025-01-01T00:00:00Z"

    with patch.object(order_service, "order_repository") as mock_repo:
        mock_repo.list_recent = AsyncMock(return_value=[mock_order])
        result = await order_service.list_recent_orders(session, limit=10)

    assert len(result.items) == 1


async def test_list_user_orders() -> None:
    from slowquery_demo.services import order_service

    session = AsyncMock()
    user_id = uuid.uuid4()
    mock_order = MagicMock()
    mock_order.id = uuid.uuid4()
    mock_order.user_id = user_id
    mock_order.status = "shipped"
    mock_order.total_cents = 2000
    mock_order.created_at = "2025-01-02T00:00:00Z"

    with patch.object(order_service, "order_repository") as mock_repo:
        mock_repo.list_for_user = AsyncMock(return_value=[mock_order])
        result = await order_service.list_user_orders(session, user_id, limit=10)

    assert len(result.items) == 1


async def test_list_items_for_product() -> None:
    from slowquery_demo.services import order_service

    session = AsyncMock()
    product_id = uuid.uuid4()
    mock_item = MagicMock()
    mock_item.id = uuid.uuid4()
    mock_item.order_id = uuid.uuid4()
    mock_item.product_id = product_id
    mock_item.quantity = 1
    mock_item.unit_price_cents = 300
    mock_item.created_at = "2025-01-03T00:00:00Z"

    with patch.object(order_service, "order_item_repository") as mock_repo:
        mock_repo.list_for_product = AsyncMock(return_value=[mock_item])
        result = await order_service.list_items_for_product(session, product_id, limit=10)

    assert len(result.items) == 1


# ---------------------------------------------------------------------------
# product_service
# ---------------------------------------------------------------------------


async def test_get_product_not_found() -> None:
    from slowquery_demo.services import product_service

    session = AsyncMock()
    with patch.object(product_service, "product_repository") as mock_repo:
        mock_repo.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(ProductNotFoundError):
            await product_service.get_product(session, uuid.uuid4())


async def test_get_product_happy_path() -> None:
    from slowquery_demo.services import product_service

    session = AsyncMock()
    product_id = uuid.uuid4()
    mock_product = MagicMock()
    mock_product.id = product_id
    mock_product.sku = "WIDGET-001"
    mock_product.name = "Widget"
    mock_product.price_cents = 999
    mock_product.created_at = "2025-01-01T00:00:00Z"

    with patch.object(product_service, "product_repository") as mock_repo:
        mock_repo.get_by_id = AsyncMock(return_value=mock_product)
        result = await product_service.get_product(session, product_id)

    assert result.id == product_id


async def test_list_products() -> None:
    from slowquery_demo.services import product_service

    session = AsyncMock()
    mock_product = MagicMock()
    mock_product.id = uuid.uuid4()
    mock_product.sku = "GADGET-001"
    mock_product.name = "Gadget"
    mock_product.price_cents = 1500
    mock_product.created_at = "2025-01-01T00:00:00Z"

    with patch.object(product_service, "product_repository") as mock_repo:
        mock_repo.list_products = AsyncMock(return_value=[mock_product])
        result = await product_service.list_products(session, limit=10)

    assert len(result.items) == 1
