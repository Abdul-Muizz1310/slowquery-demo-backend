"""Routes for OrderItem."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/order_items", tags=["order_items"])


@router.get("/")
async def order_items_list() -> dict[str, str]:
    """GET /order_items"""
    return {"handler": "order_items.list"}
