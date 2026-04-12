"""FastAPI entry point for slowquery_demo.

``create_app()`` is the factory. It reads :class:`Settings`, builds an
async engine, installs the slowquery-detective middleware + dashboard
router, wires the branch switcher with a real engine-rebuild closure
(spec 10), and attaches the five MVC routers. The module-level ``app``
singleton is the target uvicorn and alembic import.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

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

logger = logging.getLogger(__name__)

_ENGINE_DISPOSE_GRACE_S = 5.0
_HEALTH_CHECK_TIMEOUT_S = 5.0


def _make_engine_builder(app: FastAPI) -> Any:
    """Return an async callable the BranchSwitcher calls on switch.

    Closes DEVIATIONS §3. The callable:
    1. Builds a new AsyncEngine + session factory via ``build_engine``.
    2. Health-checks the new engine with ``SELECT 1`` + a 5s timeout.
    3. Atomically swaps ``app.state.engine`` + ``app.state.db_sessionmaker``.
    4. Disposes the old engine after a 5-second grace period for
       in-flight queries.
    """

    async def _rebuild(url: str) -> tuple[Any, Any]:
        new_engine, new_factory = build_engine(url)

        # Health check: ``SELECT 1`` must complete within the timeout.
        from sqlalchemy import text

        async with new_engine.connect() as conn:
            await asyncio.wait_for(
                conn.execute(text("SELECT 1")),
                timeout=_HEALTH_CHECK_TIMEOUT_S,
            )

        # Atomic swap. After this line, every new request/session uses
        # the new engine. In-flight queries on the old engine are still
        # draining.
        old_engine: AsyncEngine = app.state.engine
        app.state.engine = new_engine
        app.state.db_sessionmaker = new_factory

        # Dispose old engine after a grace period so in-flight queries
        # have time to finish. Fire-and-forget.
        async def _dispose_later() -> None:
            await asyncio.sleep(_ENGINE_DISPOSE_GRACE_S)
            try:
                await old_engine.dispose()
                logger.info("disposed old engine after grace period")
            except Exception:
                logger.exception("failed to dispose old engine")

        asyncio.create_task(_dispose_later(), name="dispose_old_engine")  # noqa: RUF006
        return new_engine, new_factory

    return _rebuild


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
        engine_builder=_make_engine_builder(app),
    )

    install_slowquery(app, engine, settings)

    app.include_router(users_router)
    app.include_router(products_router)
    app.include_router(orders_router)
    app.include_router(order_items_router)
    app.include_router(branches_router)
    return app


app = create_app()
