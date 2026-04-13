"""Typed domain errors + FastAPI exception handlers.

Services raise these. Controllers never catch them directly — the
handlers registered by :func:`register_exception_handlers` map each
type to an HTTP response so the handler code path stays thin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


class DomainError(RuntimeError):
    """Base for typed domain errors raised by services."""

    status_code: int = 500
    error_code: str = "internal_error"


class UserNotFoundError(DomainError):
    status_code = 404
    error_code = "user_not_found"


class OrderNotFoundError(DomainError):
    status_code = 404
    error_code = "order_not_found"


class ProductNotFoundError(DomainError):
    status_code = 404
    error_code = "product_not_found"


class InvalidCursorError(DomainError):
    status_code = 422
    error_code = "invalid_cursor"


class ConfigError(DomainError):
    """Raised at startup when configuration is missing/invalid."""

    status_code = 500
    error_code = "config_error"


def register_exception_handlers(app: FastAPI) -> None:
    """Install handlers for :class:`DomainError` and database connectivity."""
    from sqlalchemy.exc import OperationalError

    async def _handle_domain_error(_: Request, exc: Exception) -> JSONResponse:
        if not isinstance(exc, DomainError):  # pragma: no cover - defensive
            raise exc
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.error_code},
        )

    async def _handle_db_unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"error": "service_unavailable"},
        )

    app.add_exception_handler(DomainError, _handle_domain_error)
    app.add_exception_handler(OperationalError, _handle_db_unavailable)
