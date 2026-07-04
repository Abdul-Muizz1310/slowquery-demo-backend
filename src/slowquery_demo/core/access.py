"""Access control for the state-mutating endpoints on a public URL.

Two independent, composable gates guard the only endpoints with lasting
cross-visitor side effects (``POST /branches/switch`` and
``POST /_slowquery/{id}/force-explain``). They apply *even while
``DEMO_MODE`` bypasses the platform-token middleware*, closing the two
HIGH findings from the audit:

* :func:`enforce_cooldown` — a per-client in-process cooldown so a
  visitor cannot spam engine-rebuild churn (branch switch) or repeated
  writes against free-tier Neon/Render.
* :func:`require_admin_token` — a fail-closed shared-secret gate for the
  *destructive* force-explain endpoint: with no ``DEMO_MUTATION_TOKEN``
  configured the endpoint is disabled entirely, so an anonymous visitor
  can never overwrite a genuine captured EXPLAIN plan with a stub.
* :func:`require_admin_token_if_configured` — the softer variant used by
  branch switch, whose destructive potential is transient and already
  bounded by the cooldown: public by default, locked down the moment a
  token is configured.

The gates are FastAPI dependencies reading typed
:class:`~slowquery_demo.core.config.Settings` off ``app.state`` — no raw
``os.environ`` access, no side effects in the pure check itself.
"""

from __future__ import annotations

import hmac
import time
from typing import TYPE_CHECKING

from fastapi import HTTPException, Request

if TYPE_CHECKING:
    from slowquery_demo.core.config import Settings


class CooldownLimiter:
    """Per-key monotonic-clock cooldown.

    ``allow(key)`` returns ``True`` and arms the cooldown when the key is
    outside its window, ``False`` while inside it. A ``cooldown_s`` of
    ``0`` disables the limiter (every call is allowed) so unit tests stay
    deterministic; production sets a positive value.
    """

    def __init__(self, cooldown_s: float) -> None:
        if cooldown_s < 0:
            raise ValueError("cooldown_s must be >= 0")
        self._cooldown_s = cooldown_s
        self._last_allowed: dict[str, float] = {}

    @property
    def cooldown_s(self) -> float:
        return self._cooldown_s

    def allow(self, key: str) -> bool:
        if self._cooldown_s <= 0:
            return True
        now = time.monotonic()
        last = self._last_allowed.get(key)
        if last is not None and (now - last) < self._cooldown_s:
            return False
        self._last_allowed[key] = now
        return True


def _client_key(request: Request) -> str:
    """Stable per-caller key for the cooldown (client IP, else a constant)."""
    client = request.client
    return client.host if client is not None else "unknown"


def _get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def enforce_cooldown(request: Request) -> None:
    """Reject a mutating call that arrives inside its per-client cooldown.

    Reads the shared :class:`CooldownLimiter` off ``app.state``. Absent a
    limiter (or with a zero cooldown) every call passes.
    """
    limiter: CooldownLimiter | None = getattr(request.app.state, "mutation_limiter", None)
    if limiter is None:
        return
    if not limiter.allow(_client_key(request)):
        raise HTTPException(
            status_code=429,
            detail="rate limited: this endpoint has a cooldown; retry shortly",
        )


def _token_matches(provided: str | None, configured: str) -> bool:
    return bool(provided) and hmac.compare_digest(provided or "", configured)


async def require_admin_token(request: Request) -> None:
    """Fail-closed shared-secret gate for destructive mutations.

    With no ``DEMO_MUTATION_TOKEN`` configured the endpoint is disabled
    (403) — this is the point: an anonymous visitor must never be able to
    clobber genuine captured data. When configured, the caller must send a
    matching ``X-Admin-Token`` header (compared in constant time).
    """
    configured = _get_settings(request).demo_mutation_token
    if not configured:
        raise HTTPException(
            status_code=403,
            detail="mutation disabled: DEMO_MUTATION_TOKEN is not configured",
        )
    if not _token_matches(request.headers.get("x-admin-token"), configured):
        raise HTTPException(status_code=403, detail="invalid or missing admin token")


async def require_admin_token_if_configured(request: Request) -> None:
    """Soft admin gate: enforce the shared secret only when one is set.

    Used by branch switch — public by default (its churn is bounded by the
    cooldown) but lockable by configuring ``DEMO_MUTATION_TOKEN``.
    """
    configured = _get_settings(request).demo_mutation_token
    if not configured:
        return
    if not _token_matches(request.headers.get("x-admin-token"), configured):
        raise HTTPException(status_code=403, detail="invalid or missing admin token")
