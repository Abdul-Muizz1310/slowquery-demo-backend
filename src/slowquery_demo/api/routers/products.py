"""Routes for Product."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/products", tags=["products"])


@router.get("/")
async def products_list() -> dict[str, str]:
    """GET /products"""
    return {"handler": "products.list"}
