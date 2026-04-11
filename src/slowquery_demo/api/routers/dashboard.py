"""Local slowquery dashboard router.

slowquery-detective v0.1.0 ships ``dashboard_router`` as a ``_LazyRouter``
stub that raises ``NotImplementedError`` on access. Until the library
publishes a real implementation, this local router covers the HTTP
surface the demo needs: a read-only listing at ``GET /queries`` that
returns an empty list when no fingerprints have been captured yet.

Mounted at ``/_slowquery`` by :func:`install_slowquery` in
``core/observability.py``. The prefix is deliberately non-obvious so a
casual visitor doesn't stumble onto it.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["slowquery"])


@router.get("/queries")
async def list_queries() -> list[dict[str, object]]:
    """Return the current fingerprint list.

    S4 returns an empty list (no live store wired yet). Spec 05 test 2
    pins this contract. Integration (S5) will wire a real store read.
    """
    return []
