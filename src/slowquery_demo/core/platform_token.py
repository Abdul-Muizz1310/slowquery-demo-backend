"""X-Platform-Token middleware — verifies bastion-minted Ed25519 platform JWTs.

In demo mode (the default for this deployment) the middleware accepts every
request and logs a one-time warning. When demo mode is off AND a bastion public
key is configured — either ``BASTION_SIGNING_KEY_PUBLIC`` (base64 DER) or fetched
once from ``BASTION_PUBLIC_KEY_URL`` and cached for an hour — it verifies the
EdDSA-signed ``X-Platform-Token`` on every non-exempt route and rejects invalid
or missing tokens with 401. The platform and docs endpoints are always exempt.

Enforcement is opt-in: with no key configured the middleware fails open, since
bastion is the only minter and frontends call this service directly in the
demo deployment. Token format is bastion's ``{sub, role, service, iat, exp}``
EdDSA JWT (see bastion ``src/lib/gateway/jwt.ts``).

When enforcement is on, claims are validated against optional env config so a
token minted for a *different* sibling service (cross-service replay) is
rejected: ``BASTION_EXPECTED_ISSUER`` / ``BASTION_EXPECTED_AUDIENCE`` are passed
to ``jwt.decode``, ``BASTION_EXPECTED_SERVICE`` must equal the token's
``service`` claim, and ``BASTION_ALLOWED_ROLES`` (comma-separated) constrains
the ``role`` claim.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import jwt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_Handler = Callable[[Request], Awaitable[Response]]

_HEADER = "x-platform-token"
_EXEMPT_EXACT = frozenset({"/", "/health", "/version", "/metrics", "/openapi.json"})
_EXEMPT_PREFIXES = ("/docs", "/redoc")
_PUBLIC_KEY_TTL_S = 3600.0

# (fetched_at_monotonic, pem) — only used for the BASTION_PUBLIC_KEY_URL path.
_key_cache: tuple[float, str | None] = (0.0, None)


def reset_public_key_cache() -> None:
    """Clear the fetched-public-key cache (test seam)."""
    global _key_cache
    _key_cache = (0.0, None)


def _wrap_pem(b64_der: str) -> str:
    return f"-----BEGIN PUBLIC KEY-----\n{b64_der.strip()}\n-----END PUBLIC KEY-----\n"


async def load_public_key_pem() -> str | None:
    """Resolve bastion's Ed25519 public key as PEM (env first, then cached URL).

    Async so the URL fetch never blocks the event loop (P10): the key is
    fetched with :class:`httpx.AsyncClient` and cached for an hour, so the
    single-worker uvicorn loop is not stalled on a synchronous ``httpx.get``.
    """
    raw = os.environ.get("BASTION_SIGNING_KEY_PUBLIC")
    if raw:
        return _wrap_pem(raw)

    url = os.environ.get("BASTION_PUBLIC_KEY_URL")
    if not url:
        return None

    global _key_cache
    cached_at, cached = _key_cache
    now = time.monotonic()
    if cached is not None and (now - cached_at) < _PUBLIC_KEY_TTL_S:
        return cached
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url)
        resp.raise_for_status()
        pem = _wrap_pem(str(resp.json()["publicKey"]))
    except Exception:  # pragma: no cover - network failure path
        logger.warning("could not fetch bastion public key from %s", url)
        return None
    _key_cache = (now, pem)
    return pem


def _is_exempt(path: str) -> bool:
    return path in _EXEMPT_EXACT or path.startswith(_EXEMPT_PREFIXES)


def _claims_ok(payload: dict[str, Any]) -> bool:
    """Assert service/role claims when the corresponding env is configured.

    Cross-service replay defence (SEC-1): a signature-valid token minted
    for another sibling service or a disallowed role is rejected once the
    operator declares what this service expects. Absent config, this is a
    no-op so the fail-open demo posture is unchanged.
    """
    expected_service = os.environ.get("BASTION_EXPECTED_SERVICE")
    if expected_service and payload.get("service") != expected_service:
        return False
    allowed_roles_raw = os.environ.get("BASTION_ALLOWED_ROLES")
    if allowed_roles_raw:
        allowed = {r.strip() for r in allowed_roles_raw.split(",") if r.strip()}
        if payload.get("role") not in allowed:
            return False
    return True


def _verify(token: str, pem: str) -> bool:
    decode_kwargs: dict[str, Any] = {"algorithms": ["EdDSA"]}
    issuer = os.environ.get("BASTION_EXPECTED_ISSUER")
    if issuer:
        decode_kwargs["issuer"] = issuer
    audience = os.environ.get("BASTION_EXPECTED_AUDIENCE")
    if audience:
        decode_kwargs["audience"] = audience
    try:
        payload = jwt.decode(token, pem, **decode_kwargs)
    except Exception:
        return False
    return _claims_ok(payload)


def install_platform_token(app: FastAPI, *, demo_mode: bool) -> None:
    """Attach the X-Platform-Token verification middleware to ``app``."""
    if demo_mode:
        logger.warning(
            "DEMO_MODE active: X-Platform-Token validation is bypassed; accepting all requests"
        )

    @app.middleware("http")
    async def _platform_token_middleware(request: Request, call_next: _Handler) -> Response:
        if demo_mode or _is_exempt(request.url.path):
            return await call_next(request)
        pem = await load_public_key_pem()
        if pem is None:
            # Enforcement is opt-in; with no key configured we fail open.
            return await call_next(request)
        token = request.headers.get(_HEADER)
        if not token or not _verify(token, pem):
            return JSONResponse({"error": "invalid platform token"}, status_code=401)
        return await call_next(request)
