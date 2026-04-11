"""Platform middleware shared across feathers-generated services.

Provides the cross-cutting concerns every backend needs to be discoverable by a
control plane:

- ``X-Request-Id`` propagation (generated if absent, echoed on response)
- ``X-Platform-Token`` placeholder (demo mode accepts any token)
- ``/health`` — liveness probe
- ``/version`` — build identifier
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_Handler = Callable[[Request], Awaitable[Response]]


def install_platform_middleware(app: FastAPI, *, service_name: str) -> None:
    """Attach platform endpoints and request-id middleware to ``app``."""

    @app.middleware("http")
    async def _request_id_middleware(request: Request, call_next: _Handler) -> Response:
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.get("/health", include_in_schema=False)
    async def _health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": service_name,
                "version": "0.1.0",
                "db": "unknown",
            }
        )

    @app.get("/version", include_in_schema=False)
    async def _version() -> JSONResponse:
        return JSONResponse({"service": service_name, "version": "0.1.0"})
