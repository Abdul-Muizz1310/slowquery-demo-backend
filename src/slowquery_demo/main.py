"""FastAPI entry point for slowquery_demo.

``create_app()`` is the factory. It reads :class:`Settings`, builds an
async engine, installs the slowquery-detective middleware + dashboard
router, wires the branch switcher, and attaches the five MVC routers.
The module-level ``app`` singleton is the target uvicorn and alembic
import.
"""

from __future__ import annotations

from fastapi import FastAPI

from slowquery_demo.api.routers.branches import router as branches_router
from slowquery_demo.api.routers.order_items import router as order_items_router
from slowquery_demo.api.routers.orders import router as orders_router
from slowquery_demo.api.routers.products import router as products_router
from slowquery_demo.api.routers.users import router as users_router
from slowquery_demo.core.branch_state import load_branch
from slowquery_demo.core.config import Settings
from slowquery_demo.core.database import build_engine
from slowquery_demo.core.errors import register_exception_handlers
from slowquery_demo.core.observability import install_slowquery, slowquery_lifespan
from slowquery_demo.core.platform import install_platform_middleware
from slowquery_demo.services.branch_switcher import BranchSwitcher


def create_app() -> FastAPI:
    settings = Settings()
    engine, session_factory = build_engine(settings.database_url)

    app = FastAPI(title="slowquery_demo", version="0.1.0", lifespan=slowquery_lifespan)
    install_platform_middleware(app, service_name="slowquery_demo")
    register_exception_handlers(app)

    app.state.settings = settings
    app.state.engine = engine
    app.state.db_sessionmaker = session_factory
    app.state.branch_current = load_branch()
    app.state.branch_switcher = BranchSwitcher(
        initial=app.state.branch_current,
        slow_url=settings.database_url,
        fast_url=settings.database_url_fast,
    )

    install_slowquery(app, engine, settings)

    app.include_router(users_router)
    app.include_router(products_router)
    app.include_router(orders_router)
    app.include_router(order_items_router)
    app.include_router(branches_router)
    return app


app = create_app()
