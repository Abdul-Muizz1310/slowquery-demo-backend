"""/products routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.database import get_db
from slowquery_demo.schemas.pagination import PaginatedResponse
from slowquery_demo.schemas.product import ProductDTO
from slowquery_demo.services import product_service

router = APIRouter(prefix="/products", tags=["products"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
Limit = Annotated[int | None, Query()]


@router.get("", response_model=PaginatedResponse[ProductDTO])
async def list_products(session: DbSession, limit: Limit = None) -> PaginatedResponse[ProductDTO]:
    return await product_service.list_products(session, limit=limit)


@router.get("/{product_id}", response_model=ProductDTO)
async def get_product(product_id: uuid.UUID, session: DbSession) -> ProductDTO:
    return await product_service.get_product(session, product_id)
