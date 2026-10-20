"""FastAPI entry point for slowquery_demo.

``create_app()`` is the factory. It reads :class:`Settings`, builds the
commerce engine and the uninstrumented health engine on one
:class:`BranchTarget` (the persisted active branch), wires the branch
switcher to repoint that target (spec 15), attaches the five MVC routers and
the demo's own ``/_slowquery`` extras, and finally installs
slowquery-detective 0.2 on the commerce engine. The module-level ``app``
singleton is the target uvicorn imports.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from slowquery_demo.api.routers.branches import router as branches_router
from slowquery_demo.api.routers.dashboard import router as dashboard_router
from slowquery_demo.api.routers.order_items import router as order_items_router
from slowquery_demo.api.routers.orders import router as orders_router
from slowquery_demo.api.routers.products import router as products_router
from slowquery_demo.api.routers.users import router as users_router
from slowquery_demo.core.access import CooldownLimiter
from slowquery_demo.core.branch_state import load_branch
from slowquery_demo.core.config import Settings
from slowquery_demo.core.database import (
    BranchTarget,
    build_branch_engine,
    build_session_factory,
    check_branch_reachable,
)
from slowquery_demo.core.errors import register_exception_handlers
from slowquery_demo.core.observability import MOUNT_PATH, install_slowquery
from slowquery_demo.core.platform import install_platform_middleware, resolve_cors_origins
from slowquery_demo.core.platform_auth import install_platform_auth
from slowquery_demo.services.branch_switcher import BranchSwitcher, PostSwitch, Retarget

logger = logging.getLogger(__name__)

__all__ = ["app", "check_branch_reachable", "create_app"]


def _make_retarget(app: FastAPI) -> Retarget:
    """Point every new connection at another branch (spec 15).

    The target is health-checked first (``check_branch_reachable``, looked up
    on this module at call time); a failure raises before anything changes.
    Then the URL flips and both pools are disposed: connections in use finish
    where they are, every new one dials the target. The commerce engine
    object never changes, so slowquery-detective's hooks and EXPLAIN worker
    stay bound to it.
    """

    async def _retarget(url: str) -> None:
        await check_branch_reachable(url)
        app.state.branch_target.url = url
        await app.state.engine.dispose()
        await app.state.health_engine.dispose()

    return _retarget


def _make_post_switch(app: FastAPI) -> PostSwitch:
    """Clear the library's rolling percentiles after a switch (spec 06 invariant 5)."""

    async def _post_switch() -> None:
        buffer = getattr(app.state, "slowquery_buffer", None)
        if buffer is not None:
            buffer.clear()

    return _post_switch


def create_app() -> FastAPI:
    settings = Settings()
    active = load_branch()
    target = BranchTarget(settings.database_url if active == "slow" else settings.database_url_fast)
    engine = build_branch_engine(target)
    health_engine = build_branch_engine(target, pool_size=1, max_overflow=1)

    app = FastAPI(title="slowquery_demo", version="0.1.0")
    # Starlette runs the last-added middleware first. The token check goes in
    # first so CORS and the request id wrap it: a preflight never reaches it,
    # and a 401 still carries CORS headers and an X-Request-Id.
    install_platform_auth(app, settings=settings)
    install_platform_middleware(
        app,
        service_name="slowquery_demo",
        cors_origins=resolve_cors_origins(settings.cors_origins, app_env=settings.app_env),
    )
    register_exception_handlers(app)

    app.state.settings = settings
    app.state.branch_target = target
    app.state.engine = engine
    app.state.health_engine = health_engine
    app.state.db_sessionmaker = build_session_factory(engine)
    app.state.mutation_limiter = CooldownLimiter(settings.demo_mutation_cooldown_s)
    app.state.branch_current = active
    app.state.branch_switcher = BranchSwitcher(
        initial=active,
        slow_url=settings.database_url,
        fast_url=settings.database_url_fast,
        retarget=_make_retarget(app),
        post_switch=_make_post_switch(app),
    )

    app.include_router(users_router)
    app.include_router(products_router)
    app.include_router(orders_router)
    app.include_router(order_items_router)
    app.include_router(branches_router)
    app.include_router(dashboard_router, prefix=MOUNT_PATH)

    # Last: the library's request tagger must wrap the host's middleware.
    install_slowquery(app, engine, settings)
    return app


app = create_app()
