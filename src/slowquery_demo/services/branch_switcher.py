"""Branch-swap business logic (spec 06 + spec 15).

The switcher owns a lock that serializes concurrent switch requests and
tracks the currently-active branch. Moving the connections is delegated to a
``retarget`` async callable provided by ``main.py``: it health-checks the
target URL, points the app's engines at it and disposes their pools (spec
15; spec 10's engine rebuild is superseded, because slowquery-detective is
bound to one engine for the app's lifetime).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from slowquery_demo.core.branch_state import BranchName, save_branch

logger = logging.getLogger(__name__)

# Receives the target URL; raises when the target is unreachable so the
# switch aborts with nothing changed.
Retarget = Callable[[str], Awaitable[None]]

# Runs after a successful retarget (clears the rolling percentiles). It owns
# no HTTP or FastAPI concepts — ``main.py`` injects a closure over the app.
PostSwitch = Callable[[], Awaitable[None]]


class BranchSwitcher:
    """Owns the currently-active branch and the switch lock."""

    def __init__(
        self,
        *,
        initial: BranchName,
        slow_url: str,
        fast_url: str,
        retarget: Retarget | None = None,
        post_switch: PostSwitch | None = None,
    ) -> None:
        self._active: BranchName = initial
        self._slow_url = slow_url
        self._fast_url = fast_url
        self._retarget = retarget
        self._post_switch = post_switch
        self._lock = asyncio.Lock()

    @property
    def active(self) -> BranchName:
        return self._active

    async def switch(self, target: BranchName) -> tuple[datetime, int]:
        """Swap the active branch.

        Returns the (``switched_at``, ``latency_ms``) pair the handler
        puts in the response body. Raises :class:`ValueError` if
        ``target`` already is the active branch — the handler maps
        that to a 409 response.

        When ``retarget`` is provided (production path) it moves the app's
        connections to the target branch first; if it raises, nothing changes.
        After it succeeds the ``post_switch`` hook (if provided) clears the
        rolling percentiles (spec 06 invariant 5). A state-only switch (no
        ``retarget``, unit tests) records no new samples to invalidate.
        """
        async with self._lock:
            if target == self._active:
                raise ValueError(f"already on {target}")
            start = time.monotonic()
            if self._retarget is not None:
                url = self._slow_url if target == "slow" else self._fast_url
                await self._retarget(url)
                logger.info("connections repointed to branch=%s", target)
                if self._post_switch is not None:
                    await self._post_switch()
            self._active = target
            save_branch(target)
            latency_ms = max(1, int((time.monotonic() - start) * 1000))
            return datetime.now(UTC), latency_ms
