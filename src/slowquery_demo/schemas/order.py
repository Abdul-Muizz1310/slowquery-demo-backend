"""Order DTOs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

OrderStatus = Literal["pending", "paid", "shipped", "cancelled"]


class OrderItemDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    order_id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    unit_price_cents: int


class OrderDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    status: OrderStatus
    total_cents: int
    created_at: datetime


class OrderWithItemsDTO(OrderDTO):
    items: list[OrderItemDTO] = []
