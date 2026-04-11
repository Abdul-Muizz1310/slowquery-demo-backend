"""FastAPI entry point for slowquery_demo."""

from __future__ import annotations

from fastapi import FastAPI

from slowquery_demo.api.routers.order_items import router as order_items_router
from slowquery_demo.api.routers.orders import router as orders_router
from slowquery_demo.api.routers.products import router as products_router
from slowquery_demo.api.routers.users import router as users_router
from slowquery_demo.core.platform import install_platform_middleware

app = FastAPI(title="slowquery_demo", version="0.1.0")
install_platform_middleware(app, service_name="slowquery_demo")


app.include_router(users_router)
app.include_router(products_router)
app.include_router(orders_router)
app.include_router(order_items_router)
