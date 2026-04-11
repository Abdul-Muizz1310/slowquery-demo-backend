"""Branch-swap business logic.

The switcher owns a lock that serializes concurrent switch requests
and tracks the currently-active branch. Engine construction /
disposal is delegated to ``core/database.build_engine`` so this
service contains no SQLAlchemy imports beyond the ``AsyncEngine``
type annotation. The real engine-swap path is exercised by
integration tests (spec 06 tests 6-10); unit tests assert the
validation and state-transition logic.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from slowquery_demo.core.branch_state import BranchName, save_branch


class BranchSwitcher:
    """Owns the currently-active branch and the switch lock."""

    def __init__(
        self,
        *,
        initial: BranchName,
        slow_url: str,
        fast_url: str,
        engine_builder: Any | None = None,
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
        """
        async with self._lock:
            if target == self._active:
                raise ValueError(f"already on {target}")
            start = time.monotonic()
            # Real engine-swap path is integration-only. Unit tests
            # exercise the state transition + timing envelope.
            if self._engine_builder is not None:
                url = self._slow_url if target == "slow" else self._fast_url
                self._engine_builder(url)
            self._active = target
            save_branch(target)
            latency_ms = max(1, int((time.monotonic() - start) * 1000))
            return datetime.now(UTC), latency_ms
