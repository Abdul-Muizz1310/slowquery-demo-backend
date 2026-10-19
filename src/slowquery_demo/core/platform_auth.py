"""The platform-token middleware: contract v2 per request (spec 14).

The imperative shell around :mod:`slowquery_demo.core.platform_token`:

1. public route (:func:`~slowquery_demo.core.public_routes.is_public`) → pass;
2. no trusted key and not the test settings (``APP_ENV=test``) → 503
   ``platform_auth_unconfigured`` (fail closed, whatever ``DEMO_MODE`` says);
3. no token → 401 ``missing_platform_token``, unless ``DEMO_MODE`` lets the
   request through anonymous with a WARNING;
4. a present token that fails any check → 401 ``invalid_platform_token`` and
   a WARNING naming the check (never the token), even under ``DEMO_MODE``;
5. otherwise the principal goes on ``request.state.platform_principal``.

The token is read from ``X-Platform-Token``, else ``Authorization: Bearer``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from slowquery_demo.core.config import Settings
from slowquery_demo.core.platform_keys import load_trusted_keys
from slowquery_demo.core.platform_token import InvalidPlatformTokenError, verify_platform_token
from slowquery_demo.core.public_routes import is_public

logger = logging.getLogger(__name__)

_Handler = Callable[[Request], Awaitable[Response]]

TOKEN_HEADER: Final = "x-platform-token"
_BEARER: Final = "bearer "


def read_token(request: Request) -> str | None:
    """``X-Platform-Token`` first, then an ``Authorization: Bearer`` token."""
    header = request.headers.get(TOKEN_HEADER, "").strip()
    if header:
        return header
    authorization = request.headers.get("authorization", "").strip()
    if authorization.lower().startswith(_BEARER):
        bearer = authorization[len(_BEARER) :].strip()
        return bearer or None
    return None


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


def install_platform_auth(app: FastAPI, *, settings: Settings) -> None:
    """Attach the contract-v2 middleware. Install it before CORS, so CORS wraps it."""
    test_settings = settings.app_env.strip().lower() == "test"
    if settings.demo_mode:
        logger.warning("DEMO_MODE active: requests without a platform token proceed anonymous")

    @app.middleware("http")
    async def _platform_auth(request: Request, call_next: _Handler) -> Response:
        if is_public(request.method, request.url.path):
            return await call_next(request)

        keys = await load_trusted_keys(settings)
        if not keys and not test_settings:
            logger.error("no trusted platform key configured; refusing %s", request.url.path)
            return _deny(503, "platform_auth_unconfigured")

        token = read_token(request)
        if token is None:
            if settings.demo_mode:
                logger.warning(
                    "DEMO_MODE: %s %s without a platform token proceeds anonymous",
                    request.method,
                    request.url.path,
                )
                return await call_next(request)
            return _deny(401, "missing_platform_token")

        try:
            principal = verify_platform_token(token, keys, now=time.time())
        except InvalidPlatformTokenError as exc:
            logger.warning(
                "platform token rejected (%s) on %s %s",
                exc.check.value,
                request.method,
                request.url.path,
            )
            return _deny(401, "invalid_platform_token")

        request.state.platform_principal = principal
        return await call_next(request)
