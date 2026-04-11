"""FastAPI entry point for slowquery_demo.

``create_app()`` is the factory; ``app`` is a module-level instance for
uvicorn / alembic / tests. Splitting factory from instance lets tests
build fresh apps with overridden settings without tearing down a
singleton.
"""

from __future__ import annotations

from fastapi import FastAPI

from slowquery_demo.api.routers.order_items import router as order_items_router
from slowquery_demo.api.routers.orders import router as orders_router
from slowquery_demo.api.routers.products import router as products_router
from slowquery_demo.api.routers.users import router as users_router
from slowquery_demo.core.errors import register_exception_handlers
from slowquery_demo.core.platform import install_platform_middleware


def create_app() -> FastAPI:
    app = FastAPI(title="slowquery_demo", version="0.1.0")
    install_platform_middleware(app, service_name="slowquery_demo")
    register_exception_handlers(app)
    app.include_router(users_router)
    app.include_router(products_router)
    app.include_router(orders_router)
    app.include_router(order_items_router)
    return app


app = create_app()
