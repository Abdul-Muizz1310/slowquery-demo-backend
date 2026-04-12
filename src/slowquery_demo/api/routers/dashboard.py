"""Dashboard API at ``/_slowquery`` (specs 08 + 09).

Reads the bookkeeping tables populated by the drainer
(``core/observability.py``) and returns the data the Phase 4c
dashboard frontend needs. Also serves an SSE stream at ``/api/stream``
(spec 09) that pushes ``tick`` / ``heartbeat`` / ``branch_switched``
events to the dashboard's live timeline chart.

Mounted at ``/_slowquery`` by :func:`install_slowquery` in
``core/observability.py``.
"""

from __future__ import annotations

import asyncio
import collections.abc
import json
import re
from datetime import UTC
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from slowquery_demo.core.database import get_db
from slowquery_demo.repositories import slowquery_repository as repo
from slowquery_demo.schemas.slowquery import (
    FingerprintDetailResponse,
    FingerprintResponse,
    SuggestionResponse,
)

router = APIRouter(tags=["slowquery"])

DbSession = Annotated[AsyncSession, Depends(get_db)]

_FINGERPRINT_ID_RE = re.compile(r"^[a-f0-9]{1,16}$")


def _extract_rule(suggestion_rationale: str, suggestion_source: str) -> str | None:
    """Derive the rule name for rule-sourced suggestions.

    The library's ``run_rules`` stores the rule name at the start of the
    rationale (e.g. "Seq Scan on orders with WHERE user_id; estimated
    100,000 rows"). We can't recover it perfectly from the rationale
    alone, so we expose the ``source`` field and let the frontend use
    that for badge colouring. For a best-effort rule name, we check the
    suggestion's ``kind`` + ``source`` + DDL prefix.
    """
    if suggestion_source != "rules":
        return None
    # The rules engine in slowquery-detective names its suggestions via
    # the class attribute. We can't recover the exact name from the
    # rationale, but the drainer could stash it. For now return None
    # and let the frontend fall back to the kind.
    return None


@router.get("/queries")
async def list_queries(session: DbSession) -> list[FingerprintResponse]:
    """Return all captured fingerprints sorted by total_ms desc."""
    fingerprints = await repo.list_fingerprints(session)
    all_suggestions = await repo.list_all_suggestions(session)
    return [
        FingerprintResponse.model_validate(
            fp,
            from_attributes=True,
        ).model_copy(
            update={
                "suggestions": [
                    SuggestionResponse.model_validate(s, from_attributes=True).model_copy(
                        update={"rule": _extract_rule(s.rationale, s.source)}
                    )
                    for s in all_suggestions.get(fp.id, [])
                ]
            }
        )
        for fp in fingerprints
    ]


@router.get("/queries/{fingerprint_id}")
async def get_query_detail(fingerprint_id: str, session: DbSession) -> FingerprintDetailResponse:
    """Return the full detail for one fingerprint."""
    if not _FINGERPRINT_ID_RE.match(fingerprint_id):
        raise HTTPException(status_code=404, detail="not found")

    fp = await repo.get_fingerprint_by_id(session, fingerprint_id)
    if fp is None:
        raise HTTPException(status_code=404, detail="not found")

    suggestions = await repo.list_suggestions_for_fingerprint(session, fingerprint_id)
    plan = await repo.get_explain_plan(session, fingerprint_id)
    samples = await repo.list_recent_samples(session, fingerprint_id)

    suggestion_dtos = [
        SuggestionResponse.model_validate(s, from_attributes=True).model_copy(
            update={"rule": _extract_rule(s.rationale, s.source)}
        )
        for s in suggestions
    ]

    fp_dto = FingerprintResponse.model_validate(fp, from_attributes=True).model_copy(
        update={"suggestions": suggestion_dtos}
    )

    from slowquery_demo.schemas.slowquery import (
        ExplainPlanResponse,
        QuerySampleResponse,
    )

    return FingerprintDetailResponse(
        fingerprint=fp_dto,
        canonical_sql=fp.fingerprint,
        explain_plan=(
            ExplainPlanResponse.model_validate(plan, from_attributes=True) if plan else None
        ),
        suggestions=suggestion_dtos,
        recent_samples=[
            QuerySampleResponse.model_validate(s, from_attributes=True) for s in samples
        ],
    )


# ---------------------------------------------------------------------------
# Spec 09 — SSE stream
# ---------------------------------------------------------------------------

_SSE_POLL_INTERVAL_S = 2.0


async def _sse_generator(
    request: Request,
    session: AsyncSession,
) -> collections.abc.AsyncGenerator[str, None]:
    """Polling-backed SSE generator.

    Emits ``tick`` events when a fingerprint's p95 changes, and
    ``heartbeat`` events when nothing changed. Uses a single session
    (resolved through ``get_db`` dependency injection so test overrides
    apply) and re-queries the table on each poll tick.
    """
    from datetime import datetime

    last_p95: dict[str, float | None] = {}

    # Emit an initial batch immediately so the client gets data before
    # the first poll interval elapses.
    fps = await repo.list_fingerprints(session)
    now_iso = datetime.now(UTC).isoformat()
    if fps:
        for fp in fps:
            current = float(fp.p95_ms) if fp.p95_ms is not None else None
            if current is not None:
                event = {
                    "kind": "tick",
                    "fingerprint_id": fp.id,
                    "p95_ms": current,
                    "sampled_at": now_iso,
                }
                yield f"data: {json.dumps(event)}\n\n"
            last_p95[fp.id] = current
    else:
        heartbeat = {"kind": "heartbeat", "now": now_iso}
        yield f"data: {json.dumps(heartbeat)}\n\n"

    while True:
        await asyncio.sleep(_SSE_POLL_INTERVAL_S)
        if await request.is_disconnected():
            return

        fps = await repo.list_fingerprints(session)
        now_iso = datetime.now(UTC).isoformat()
        emitted = False

        for fp in fps:
            prev = last_p95.get(fp.id)
            current = float(fp.p95_ms) if fp.p95_ms is not None else None
            if current is not None and current != prev:
                event = {
                    "kind": "tick",
                    "fingerprint_id": fp.id,
                    "p95_ms": current,
                    "sampled_at": now_iso,
                }
                yield f"data: {json.dumps(event)}\n\n"
                emitted = True
            last_p95[fp.id] = current

        if not emitted:
            heartbeat = {"kind": "heartbeat", "now": now_iso}
            yield f"data: {json.dumps(heartbeat)}\n\n"


@router.get("/api/stream")
async def stream_fingerprints(request: Request, session: DbSession) -> StreamingResponse:
    """SSE endpoint consumed by the Phase 4c dashboard's LiveTimeline."""
    return StreamingResponse(
        _sse_generator(request, session),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
