"""``POST /branches/switch`` — swap the active Neon branch."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Request

from slowquery_demo.schemas.branches import SwitchBranchRequest, SwitchBranchResponse
from slowquery_demo.services.branch_switcher import BranchSwitcher

router = APIRouter(prefix="/branches", tags=["branches"])


def _get_switcher(request: Request) -> BranchSwitcher:
    switcher = getattr(request.app.state, "branch_switcher", None)
    if switcher is None:
        raise HTTPException(status_code=503, detail="branch_switcher not wired at app startup")
    return switcher  # type: ignore[no-any-return]


@router.post("/switch", response_model=SwitchBranchResponse)
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
