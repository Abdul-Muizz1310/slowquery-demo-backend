"""The demo's own routes under ``/_slowquery`` (specs 09, 11, 15).

slowquery-detective 0.2 owns ``GET /_slowquery/queries`` and
``GET /_slowquery/queries/{id}`` (mounted by ``install``) and removed the 0.1
stream and apply routes. The dashboard still needs two more, which the demo
keeps on top of the library's store:

* ``GET /api/stream`` — the SSE stream the live timeline plots (spec 09);
* ``POST /queries/{id}/force-explain`` — the gated "re-analyze" action
  (specs 05, 11).

Mounted at ``/_slowquery`` by ``main.create_app``; behind the platform token
like every other route there (spec 14).
"""

from __future__ import annotations

import asyncio
import collections.abc
import json
import re
from datetime import UTC, datetime
from typing import Any, Final

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from slowquery_detective import QueryRow
from slowquery_detective.llm_explainer import explain as llm_explain
from slowquery_detective.rules import run_rules

from slowquery_demo.core.access import enforce_cooldown, require_admin_token

router = APIRouter(tags=["slowquery"])

_FINGERPRINT_ID_RE: Final = re.compile(r"^[a-f0-9]{16}$")
_STREAM_LIMIT: Final = 500

# The synthetic plan force-explain stores: a bare Result node that no rule
# matches, so the LLM fallback (when enabled) is what gets exercised.
_SYNTHETIC_PLAN: Final[dict[str, Any]] = {"Plan": {"Node Type": "Result", "Total Cost": 0.01}}


def _runtime(request: Request) -> Any:
    runtime = getattr(request.app.state, "slowquery_runtime", None)
    if runtime is None:
        raise HTTPException(status_code=503, detail="slowquery not installed")
    return runtime


# ---------------------------------------------------------------------------
# Spec 05 / 11 / 15 — force-explain
# ---------------------------------------------------------------------------


@router.post(
    "/queries/{fingerprint_id}/force-explain",
    dependencies=[Depends(enforce_cooldown), Depends(require_admin_token)],
)
async def force_explain(fingerprint_id: str, request: Request) -> dict[str, str]:
    """Store a synthetic plan for a known fingerprint and re-run rules / LLM.

    Gated fail-closed behind :func:`require_admin_token` (spec 11): with no
    ``DEMO_MUTATION_TOKEN`` it answers 403 whatever the platform token says.
    Since spec 15 it acts only on a fingerprint the library has recorded
    (unknown ⇒ 404) and writes through the library's store: the plan with
    ``upsert_plan``, suggestions with ``insert_suggestions``.
    """
    if not _FINGERPRINT_ID_RE.match(fingerprint_id):
        raise HTTPException(status_code=404, detail="not found")
    runtime = _runtime(request)
    if not await runtime.ensure_ready():
        raise HTTPException(status_code=503, detail="slowquery store unavailable")
    detail = await runtime.store.get_query(runtime.service, fingerprint_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="not found")

    await runtime.store.upsert_plan(fingerprint_id, _SYNTHETIC_PLAN, 0.01, datetime.now(UTC))
    suggestions = run_rules(_SYNTHETIC_PLAN, detail.canonical_sql, fingerprint_id=fingerprint_id)

    llm_config = getattr(request.app.state, "llm_config", None)
    if not suggestions and llm_config is not None:
        async with httpx.AsyncClient() as client:
            found = await llm_explain(
                detail.canonical_sql,
                _SYNTHETIC_PLAN,
                config=llm_config,
                fingerprint_id=fingerprint_id,
                client=client,
            )
        if found is not None:
            suggestions = [found]

    if suggestions:
        await runtime.store.insert_suggestions(fingerprint_id, suggestions)
    return {"status": "ok", "suggestions_count": str(len(suggestions))}


# ---------------------------------------------------------------------------
# Spec 09 — SSE stream
# ---------------------------------------------------------------------------

_SSE_POLL_INTERVAL_S = 2.0


async def _poll_fingerprints(request: Request) -> list[QueryRow]:
    """Read the fingerprints' p95 from slowquery-detective's store (spec 15).

    The store runs on the library's own small engine, so the stream never
    pins a commerce connection (and is never itself observed). A store that
    cannot start yet (Neon asleep) yields no rows, which the generator turns
    into a heartbeat rather than an error.
    """
    runtime = _runtime(request)
    if not await runtime.ensure_ready():
        return []
    rows: list[QueryRow] = await runtime.store.list_queries(
        runtime.service, since=None, limit=_STREAM_LIMIT
    )
    return rows


async def _sse_generator(
    request: Request,
) -> collections.abc.AsyncGenerator[str, None]:
    """Polling-backed SSE generator.

    Emits ``tick`` events when a fingerprint's p95 changes, and
    ``heartbeat`` events when nothing changed. Acquires a session per
    poll tick (see :func:`_poll_fingerprints`) instead of holding one
    open for the whole connection.
    """
    last_p95: dict[str, float | None] = {}

    # Emit an initial batch immediately so the client gets data before
    # the first poll interval elapses.
    fps = await _poll_fingerprints(request)
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

        fps = await _poll_fingerprints(request)
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
async def stream_fingerprints(request: Request) -> StreamingResponse:
    """SSE endpoint consumed by the Phase 4c dashboard's LiveTimeline."""
    return StreamingResponse(
        _sse_generator(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
