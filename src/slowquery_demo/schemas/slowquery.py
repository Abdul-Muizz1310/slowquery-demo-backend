"""Response DTOs for the /_slowquery dashboard API (spec 08).

These shapes match the Zod schemas pinned in
``slowquery-dashboard-frontend/src/lib/api/schemas.ts`` so a contract
drift surfaces as a Zod parse error on the frontend, not as a silent
type hole.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SuggestionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fingerprint_id: str
    kind: str
    source: str
    rule: str | None = None
    sql: str | None
    rationale: str
    applied_at: datetime | None


class ExplainPlanResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    fingerprint_id: str
    plan_json: dict[str, object]
    plan_text: str
    cost: float | None
    captured_at: datetime


class QuerySampleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fingerprint_id: str
    params: dict[str, object] | None
    duration_ms: float
    rows: int | None
    sampled_at: datetime


class FingerprintResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    fingerprint: str
    first_seen: datetime
    last_seen: datetime
    call_count: int
    total_ms: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    suggestions: list[SuggestionResponse] = []


class FingerprintDetailResponse(BaseModel):
    fingerprint: FingerprintResponse
    canonical_sql: str
    explain_plan: ExplainPlanResponse | None
    suggestions: list[SuggestionResponse]
    recent_samples: list[QuerySampleResponse]
