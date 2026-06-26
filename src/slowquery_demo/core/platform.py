"""Platform middleware shared across feathers-generated services.

Provides the cross-cutting concerns every backend needs to be discoverable by a
control plane:

- ``X-Request-Id`` propagation (generated if absent, echoed on response)
- ``X-Platform-Token`` placeholder (demo mode accepts any token)
- ``/health`` — readiness probe (real ``SELECT 1`` against ``app.state.engine``)
- ``/version`` — build identifier
- ``/metrics`` — Prometheus exposition (prometheus-fastapi-instrumentator)
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text

_Handler = Callable[[Request], Awaitable[Response]]

_PROD_ORIGINS = [
    "https://slowquery-dashboard-frontend.vercel.app",
]

_HEALTH_CHECK_TIMEOUT_S = 5.0


def _get_cors_origins() -> list[str]:
    origins = list(_PROD_ORIGINS)
    if os.environ.get("APP_ENV", "development") != "production":
        origins.append("http://localhost:3000")
    return origins


def _resolve_version() -> str:
    """Return the package version, preferring the in-tree ``__version__``.

    Falls back to :func:`importlib.metadata.version` when the attribute is
    unavailable, and finally to ``"unknown"`` if the distribution metadata
    cannot be read (e.g. running from a source tree that was never installed).
    """
    try:
        from slowquery_demo import __version__

        return __version__
    except Exception:  # pragma: no cover - defensive, __version__ is always present
        pass
    try:  # pragma: no cover - defensive fallback, only hit if __version__ is missing
        from importlib.metadata import version

        return version("slowquery_demo")
    except Exception:
        return "unknown"


def _resolve_commit_sha() -> str:
    """Return the build commit SHA from the deploy environment.

    Render injects ``RENDER_GIT_COMMIT``; a generic ``GIT_SHA`` is honoured as
    a fallback. Returns ``"unknown"`` when neither is set.
    """
    return os.environ.get("RENDER_GIT_COMMIT") or os.environ.get("GIT_SHA", "unknown")


def install_platform_middleware(app: FastAPI, *, service_name: str) -> None:
    """Attach platform endpoints, CORS, and request-id middleware to ``app``."""

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_get_cors_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next: _Handler) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    Instrumentator().instrument(app).expose(app, include_in_schema=False, endpoint="/metrics")

    @app.get("/health", include_in_schema=False)
    async def _health(request: Request) -> JSONResponse:
        engine = getattr(request.app.state, "engine", None)
        db_ok = False
        if engine is not None:
            try:
                async with engine.connect() as conn:
                    await asyncio.wait_for(
                        conn.execute(text("SELECT 1")),
                        timeout=_HEALTH_CHECK_TIMEOUT_S,
                    )
                db_ok = True
            except Exception:
                db_ok = False

        return JSONResponse(
            status_code=200 if db_ok else 503,
            content={
                "status": "ok" if db_ok else "degraded",
                "service": service_name,
                "version": _resolve_version(),
                "commit_sha": _resolve_commit_sha(),
                "db": "ok" if db_ok else "down",
            },
        )

    @app.get("/version", include_in_schema=False)
    async def _version() -> JSONResponse:
        return JSONResponse(
            {
                "service": service_name,
                "version": _resolve_version(),
                "commit_sha": _resolve_commit_sha(),
            }
        )
