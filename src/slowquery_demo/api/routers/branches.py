"""The ``/branches`` routes.

- ``POST /branches/switch`` — swap the active Neon branch (mutating: cooldown
  + admin token when configured).
- ``GET /branches/current`` — read the active branch (side-effect free, so no
  cooldown and no token; a dashboard polls it).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from slowquery_demo.core.access import enforce_cooldown, require_admin_token_if_configured
from slowquery_demo.schemas.branches import (
    CurrentBranchResponse,
    SwitchBranchRequest,
    SwitchBranchResponse,
)
from slowquery_demo.services.branch_switcher import BranchSwitcher

router = APIRouter(prefix="/branches", tags=["branches"])


def _get_switcher(request: Request) -> BranchSwitcher:
    switcher = getattr(request.app.state, "branch_switcher", None)
    if switcher is None:
        raise HTTPException(status_code=503, detail="branch_switcher not wired at app startup")
    return switcher  # type: ignore[no-any-return]


@router.post(
    "/switch",
    response_model=SwitchBranchResponse,
    dependencies=[Depends(enforce_cooldown), Depends(require_admin_token_if_configured)],
)
async def switch_branch(
    body: SwitchBranchRequest,
    request: Annotated[Request, None] = None,  # type: ignore[assignment]
) -> SwitchBranchResponse:
    switcher = _get_switcher(request)
    try:
        switched_at, latency_ms = await switcher.switch(body.target)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return SwitchBranchResponse(
        active=switcher.active, switched_at=switched_at, latency_ms=latency_ms
    )


@router.get("/current", response_model=CurrentBranchResponse)
async def current_branch(request: Request) -> CurrentBranchResponse:
    """Return the currently-active branch.

    Read-only companion to ``POST /branches/switch``: no cooldown, no admin
    token, no side effects. Reads the same ``BranchSwitcher.active`` value a
    successful switch returns, so the two can never disagree.
    """
    switcher = _get_switcher(request)
    return CurrentBranchResponse(active=switcher.active)
