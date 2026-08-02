"""Post-deploy ``/health`` smoke check (spec 12).

Render auto-deploys ``main`` on push and CI has no deploy job, so nothing
otherwise verifies that the container that came up actually serves traffic.
This script polls the deployed ``/health`` and fails the CI step when the
service never becomes ready.

It is **inert unless configured**: with ``SMOKE_BASE_URL`` unset (or blank) it
prints a skip line, makes *zero* HTTP requests, and exits ``0``. That is
deliberate — the demo backends in this portfolio sleep, cold-boot, and
occasionally sit suspended, and none of that should turn a pull request red.

Pure predicates (:func:`health_verdict`, :func:`health_url`) are separated from
the polling shell (:func:`main`) so the interesting decisions are unit-testable
without a network.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Final

import httpx

SMOKE_URL_ENV: Final = "SMOKE_BASE_URL"
ATTEMPTS_ENV: Final = "SMOKE_ATTEMPTS"
DELAY_ENV: Final = "SMOKE_DELAY_S"

DEFAULT_ATTEMPTS: Final = 10
# Render Free cold boots take ~30s; 10 x 15s is a ~2.5 minute budget.
DEFAULT_DELAY_S: Final = 15.0
_REQUEST_TIMEOUT_S: Final = 30.0

HEALTHY: Final = "healthy"
DEGRADED: Final = "degraded"
MALFORMED: Final = "malformed"


# --- pure predicates -----------------------------------------------------


def health_verdict(status_code: int, payload: object) -> str:
    """Classify one ``/health`` response.

    Returns :data:`HEALTHY`, :data:`DEGRADED` or :data:`MALFORMED`. Fails
    closed: anything that is not a JSON object reporting ``db == "ok"``
    alongside a 2xx is *not* healthy. The status code alone is insufficient —
    a 200 whose body says ``db: down`` is a degraded service.
    """
    if not isinstance(payload, dict):
        return MALFORMED
    if 200 <= status_code < 300 and payload.get("db") == "ok":
        return HEALTHY
    return DEGRADED


def health_url(base_url: str) -> str:
    """Join ``base_url`` with ``/health`` using exactly one separator."""
    return f"{base_url.rstrip('/')}/health"


def _positive_int(raw: str | None, default: int) -> int:
    """Parse a positive int from the environment, falling back on garbage."""
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _non_negative_float(raw: str | None, default: float) -> float:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


# --- imperative shell ----------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Poll the deployed ``/health`` until healthy or the budget runs out.

    Returns ``0`` when skipped or healthy, ``1`` when the service never became
    ready. No exception escapes: transport failures are retried like a 503,
    because a cold-booting container is indistinguishable from a dead one until
    the attempts are used up.
    """
    _ = argv  # no flags: everything comes from the environment (CI-friendly)

    base_url = (os.environ.get(SMOKE_URL_ENV) or "").strip()
    if not base_url:
        print(f"smoke: {SMOKE_URL_ENV} is unset — skipping post-deploy health check.")
        return 0

    attempts = _positive_int(os.environ.get(ATTEMPTS_ENV), DEFAULT_ATTEMPTS)
    delay_s = _non_negative_float(os.environ.get(DELAY_ENV), DEFAULT_DELAY_S)
    url = health_url(base_url)

    print(f"smoke: polling {url} ({attempts} attempts, {delay_s}s apart)")
    for attempt in range(1, attempts + 1):
        verdict, detail = _probe_once(url)
        print(f"smoke: attempt {attempt}/{attempts} -> {verdict} ({detail})")
        if verdict == HEALTHY:
            print(f"smoke: {url} is healthy.")
            return 0
        if attempt < attempts and delay_s > 0:
            time.sleep(delay_s)

    print(f"smoke: {url} never became healthy within {attempts} attempts.")
    return 1


def _probe_once(url: str) -> tuple[str, str]:
    """One ``GET`` against ``url``; returns ``(verdict, human detail)``."""
    try:
        response = httpx.get(url, timeout=_REQUEST_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return DEGRADED, f"transport error: {type(exc).__name__}"

    try:
        payload: object = response.json()
    except ValueError:
        return MALFORMED, f"HTTP {response.status_code}, non-JSON body"

    verdict = health_verdict(response.status_code, payload)
    db = payload.get("db") if isinstance(payload, dict) else None
    return verdict, f"HTTP {response.status_code}, db={db!r}"


if __name__ == "__main__":
    sys.exit(main())
