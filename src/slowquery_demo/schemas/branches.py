"""Request + response schemas for the ``/branches`` routes."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

BranchName = Literal["slow", "fast"]


class SwitchBranchRequest(BaseModel):
    """Client body for ``POST /branches/switch``.

    The ``target`` field uses :class:`typing.Literal` so any value
    outside ``{"slow", "fast"}`` is rejected by Pydantic with a 422
    before the handler runs. No free-form strings ever reach the
    business logic.
    """

    model_config = ConfigDict(extra="forbid")

    target: BranchName


class SwitchBranchResponse(BaseModel):
    """Server response body."""

    active: BranchName
    switched_at: datetime
    latency_ms: int


class CurrentBranchResponse(BaseModel):
    """Server response body for ``GET /branches/current``.

    Deliberately just the active branch: the endpoint is the read-only
    companion to the switch, so it exposes exactly the state the switch
    mutates and nothing that would need a second source of truth.
    """

    active: BranchName
