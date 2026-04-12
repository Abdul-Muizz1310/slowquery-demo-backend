# Spec 09 — SSE stream endpoint (closing the SSE gap)

## Goal

Add `GET /_slowquery/api/stream` that sends Server-Sent Events to the Phase 4c dashboard. Polling-backed: re-queries `query_fingerprints` every ~2s and pushes `tick` events for any p95 changes since the last emission. Sends periodic `heartbeat` events to keep the connection alive.

## Deferred to later
- Proper change-detection (CDC / LISTEN/NOTIFY)
- Real-time from the drainer task (would need a pub/sub bridge)

## Test cases
1. SSE connection returns `text/event-stream` content type
2. First event is a batch of `tick` events for all current fingerprints
3. `heartbeat` events arrive every ~2s when no fingerprints change
4. A new fingerprint inserted between polls appears as a `tick` event
5. Connection closes cleanly on client disconnect

## Acceptance criteria
- [ ] `api/routers/dashboard.py` adds `GET /api/stream` returning `StreamingResponse`
- [ ] 5 unit tests in `tests/unit/test_09_sse_stream.py`
- [ ] Lint + mypy + tests green
