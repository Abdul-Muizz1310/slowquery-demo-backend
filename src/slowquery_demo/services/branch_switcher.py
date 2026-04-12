"""Branch-swap business logic (spec 06 + spec 10).

The switcher owns a lock that serializes concurrent switch requests
and tracks the currently-active branch. Engine construction /
disposal is delegated to an ``engine_builder`` async callable provided
by ``main.py``. When the callable is provided, switching actually
rebuilds the AsyncEngine + session factory and swaps them on
``app.state``, closing the DEVIATIONS §3 gap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from slowquery_demo.core.branch_state import BranchName, save_branch

logger = logging.getLogger(__name__)

# Type for the engine-builder closure. It receives the target URL and
# returns (new_engine, new_session_factory) — or raises on health-check
# failure so the switch aborts cleanly.
EngineBuilder = Callable[[str], Awaitable[tuple[Any, Any]]]


class BranchSwitcher:
    """Owns the currently-active branch and the switch lock."""

    def __init__(
        self,
        *,
        initial: BranchName,
        slow_url: str,
        fast_url: str,
        engine_builder: EngineBuilder | None = None,
    ) -> None:
        self._active: BranchName = initial
        self._slow_url = slow_url
        self._fast_url = fast_url
        self._engine_builder = engine_builder
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

        When ``engine_builder`` is provided (production path), the
        switcher builds a new engine against the target URL, health-
        checks it, and atomically swaps ``app.state.engine`` +
        ``app.state.db_sessionmaker``. The old engine is disposed
        asynchronously with a 5-second grace window.
        """
        async with self._lock:
            if target == self._active:
                raise ValueError(f"already on {target}")
            start = time.monotonic()
            if self._engine_builder is not None:
                url = self._slow_url if target == "slow" else self._fast_url
                await self._engine_builder(url)
                logger.info("engine rebuilt for branch=%s", target)
            self._active = target
            save_branch(target)
            latency_ms = max(1, int((time.monotonic() - start) * 1000))
            return datetime.now(UTC), latency_ms
