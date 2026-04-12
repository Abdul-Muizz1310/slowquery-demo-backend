# Spec 08 — Real dashboard read API (closing DEVIATIONS §8)

## Goal

Replace the stub `GET /_slowquery/queries` (returns `[]`) with a real implementation that reads the `query_fingerprints`, `explain_plans`, `suggestions`, and `query_samples` bookkeeping tables and returns the data the Phase 4c dashboard expects. Add `GET /_slowquery/queries/{id}` for the detail view.

## MVC shape

- **Repository:** `repositories/slowquery_repository.py` — module-level async functions following the existing pattern (`order_repository.py`, `product_repository.py`)
- **Schemas:** `schemas/slowquery.py` — Pydantic DTOs for the response shapes
- **Router:** `api/routers/dashboard.py` — expanded from the current 28-line stub

No service layer needed; this is read-only, no business logic beyond sorting.

## Endpoints

| Method | Path | Response | Notes |
|---|---|---|---|
| GET | `/_slowquery/queries` | `list[FingerprintResponse]` | sorted by `total_ms` desc, with embedded `suggestions` per fingerprint |
| GET | `/_slowquery/queries/{id}` | `FingerprintDetailResponse` | fingerprint + canonical_sql + explain_plan + suggestions + recent_samples (last 10) |

## Schemas

`FingerprintResponse`: mirrors `QueryFingerprint` ORM model + `suggestions: list[SuggestionResponse]`
`SuggestionResponse`: mirrors `Suggestion` ORM model, includes nullable `rule` derived from rationale prefix
`ExplainPlanResponse`: mirrors `ExplainPlan` ORM model
`QuerySampleResponse`: mirrors `QuerySample` ORM model
`FingerprintDetailResponse`: `{ fingerprint, canonical_sql, explain_plan | null, suggestions, recent_samples }`

These match the Zod schemas pinned in `slowquery-dashboard-frontend/src/lib/api/schemas.ts`.

## Test cases

### Happy
1. `GET /_slowquery/queries` with 3 seeded fingerprints returns 3 items sorted by `total_ms` desc
2. Each item in the list carries its `suggestions` array (possibly empty)
3. `GET /_slowquery/queries/{id}` returns the detail shape with `canonical_sql` = fingerprint text
4. Detail includes `explain_plan` when one exists
5. Detail includes up to 10 `recent_samples` in descending `sampled_at` order

### Edge
6. `GET /_slowquery/queries` with 0 fingerprints returns `[]`
7. `GET /_slowquery/queries/{id}` with no explain plan returns `explain_plan: null`
8. `GET /_slowquery/queries/{id}` with no suggestions returns `suggestions: []`
9. `GET /_slowquery/queries/{id}` with no samples returns `recent_samples: []`

### Failure
10. `GET /_slowquery/queries/{id}` with unknown id returns 404
11. `GET /_slowquery/queries/{id}` with invalid id shape returns 404

### Security
12. Fingerprint text containing `<script>` is returned as-is (JSON-encoded); the frontend handles escaping

## Acceptance criteria
- [ ] `repositories/slowquery_repository.py` exists with `list_fingerprints`, `get_fingerprint_detail` functions
- [ ] `schemas/slowquery.py` exports the five response DTOs
- [ ] `api/routers/dashboard.py` is expanded to two endpoints reading the real tables
- [ ] 12 unit tests in `tests/unit/test_08_dashboard_read_api.py`
- [ ] Lint + mypy + tests green
