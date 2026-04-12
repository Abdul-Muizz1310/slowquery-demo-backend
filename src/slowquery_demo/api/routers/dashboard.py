"""Dashboard API at ``/_slowquery`` (spec 08).

Reads the bookkeeping tables populated by the drainer
(``core/observability.py``) and returns the data the Phase 4c
dashboard frontend needs.

Mounted at ``/_slowquery`` by :func:`install_slowquery` in
``core/observability.py``.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
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
async def get_query_detail(
    fingerprint_id: str, session: DbSession
) -> FingerprintDetailResponse:
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
