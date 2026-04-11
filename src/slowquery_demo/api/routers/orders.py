"""Routes for Order."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/")
async def orders_list() -> dict[str, str]:
    """GET /orders"""
    return {"handler": "orders.list"}


@router.get("/{id}")
async def orders_get() -> dict[str, str]:
    """GET /orders/{id}"""
    return {"handler": "orders.get"}
