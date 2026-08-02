"""S4: unit-lane tests for spec 09 (SSE stream endpoint).

Driving the endpoint through Starlette's *sync* ``TestClient`` hangs — the
async generator never gets a clean close signal — so the event protocol
(spec 09 tests 1-5) is exercised by consuming ``_sse_generator`` directly with
an injected poll function and a fake ``Request``. That covers the same code the
live endpoint runs: the initial batch, the change detection, the heartbeat and
the disconnect exit.
"""

from __future__ import annotations

import json
from typing import Any


def test_sse_endpoint_registered_in_openapi(test_client) -> None:  # type: ignore[no-untyped-def]
    """Spec 09 acceptance: endpoint exists in the schema."""
    schema = test_client.get("/openapi.json").json()
    assert "/_slowquery/api/stream" in schema["paths"]


def test_sse_poll_interval_is_positive() -> None:
    """Spec 09 invariant: poll interval is a positive number."""
    from slowquery_demo.api.routers.dashboard import _SSE_POLL_INTERVAL_S

    assert _SSE_POLL_INTERVAL_S > 0


def test_sse_generator_is_async_generator() -> None:
    """Spec 09 shape: the generator is an async generator function."""
    import inspect

    from slowquery_demo.api.routers.dashboard import _sse_generator

    assert inspect.isasyncgenfunction(_sse_generator)


# --- The event protocol (spec 09 tests 1-5) -----------------------------


class _Fingerprint:
    """The two attributes ``_sse_generator`` reads off a fingerprint row."""

    def __init__(self, fp_id: str, p95_ms: float | None) -> None:
        self.id = fp_id
        self.p95_ms = p95_ms


class _FakeRequest:
    """A ``Request`` stand-in that disconnects after ``connected_polls`` ticks."""

    def __init__(self, *, connected_polls: int = 1) -> None:
        self._remaining = connected_polls

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


def _events(chunks: list[str]) -> list[dict[str, Any]]:
    """Parse ``data: {...}\\n\\n`` SSE frames into decoded payloads."""
    parsed: list[dict[str, Any]] = []
    for chunk in chunks:
        assert chunk.startswith("data: "), chunk
        assert chunk.endswith("\n\n"), chunk
        parsed.append(json.loads(chunk[len("data: ") :].strip()))
    return parsed


async def _drain(request: object, polls: list[list[_Fingerprint]], monkeypatch: Any) -> list[str]:
    """Run ``_sse_generator`` against a scripted sequence of poll results."""
    import slowquery_demo.api.routers.dashboard as dash

    remaining = list(polls)

    async def _fake_poll(_request: object) -> list[_Fingerprint]:
        return remaining.pop(0) if remaining else []

    monkeypatch.setattr(dash, "_poll_fingerprints", _fake_poll)
    monkeypatch.setattr(dash, "_SSE_POLL_INTERVAL_S", 0.0)

    return [chunk async for chunk in dash._sse_generator(request)]  # type: ignore[arg-type]


async def test_stream_endpoint_declares_the_sse_content_type() -> None:
    """Spec 09 test 1: ``text/event-stream`` with buffering disabled."""
    from unittest.mock import MagicMock

    from slowquery_demo.api.routers.dashboard import stream_fingerprints

    response = await stream_fingerprints(MagicMock())

    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


async def test_first_frame_is_a_tick_batch_for_every_current_fingerprint(  # type: ignore[no-untyped-def]
    monkeypatch,
) -> None:
    """Spec 09 test 2: the client gets data immediately, not after one interval.

    A fingerprint with a ``None`` p95 (seen but not yet percentile-computed)
    contributes no tick — there is no number to plot.
    """
    request = _FakeRequest(connected_polls=0)
    chunks = await _drain(
        request,
        [[_Fingerprint("aaa", 12.5), _Fingerprint("bbb", 30.0), _Fingerprint("ccc", None)]],
        monkeypatch,
    )

    events = _events(chunks)
    assert [e["kind"] for e in events] == ["tick", "tick"]
    assert [e["fingerprint_id"] for e in events] == ["aaa", "bbb"]
    assert [e["p95_ms"] for e in events] == [12.5, 30.0]
    assert all(e["sampled_at"] for e in events)


async def test_heartbeat_when_nothing_changed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 09 test 3: an unchanged poll emits a heartbeat, not a repeat tick.

    Also covers the empty-store case: the very first poll returning no rows
    emits a heartbeat so the client knows the stream is alive.
    """
    request = _FakeRequest(connected_polls=1)
    chunks = await _drain(
        request,
        [[_Fingerprint("aaa", 12.5)], [_Fingerprint("aaa", 12.5)]],
        monkeypatch,
    )

    events = _events(chunks)
    assert [e["kind"] for e in events] == ["tick", "heartbeat"]
    assert events[1]["now"]


async def test_empty_store_emits_only_heartbeats(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 09 test 3 (edge): no fingerprints at all is still a live stream."""
    chunks = await _drain(_FakeRequest(connected_polls=0), [[]], monkeypatch)

    assert [e["kind"] for e in _events(chunks)] == ["heartbeat"]


async def test_changed_p95_and_new_fingerprint_emit_ticks(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 09 test 4: a changed p95 and a fingerprint that appeared between
    polls both surface as ticks; an unchanged neighbour stays quiet."""
    request = _FakeRequest(connected_polls=1)
    chunks = await _drain(
        request,
        [
            [_Fingerprint("aaa", 12.5), _Fingerprint("bbb", 30.0)],
            [_Fingerprint("aaa", 99.0), _Fingerprint("bbb", 30.0), _Fingerprint("new", 5.0)],
        ],
        monkeypatch,
    )

    events = _events(chunks)
    # Frame 1: the initial batch. Frame 2: only what moved.
    assert [(e["kind"], e["fingerprint_id"]) for e in events[:2]] == [
        ("tick", "aaa"),
        ("tick", "bbb"),
    ]
    second_poll = events[2:]
    assert [(e["fingerprint_id"], e["p95_ms"]) for e in second_poll] == [
        ("aaa", 99.0),
        ("new", 5.0),
    ]


async def test_generator_stops_on_client_disconnect(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Spec 09 test 5: a disconnected client ends the generator, so the poll
    loop cannot run forever against a socket nobody is reading."""
    polls_offered = [[_Fingerprint("aaa", 1.0)]] * 5
    chunks = await _drain(_FakeRequest(connected_polls=0), polls_offered, monkeypatch)

    # Exactly the initial batch: the loop checked is_disconnected and returned.
    assert len(chunks) == 1


async def test_sse_poll_acquires_and_releases_a_session_per_tick() -> None:
    """MEDIUM fix: each poll opens a short-lived session and releases it.

    The stream must not pin a pooled connection for its whole lifetime, so
    ``_poll_fingerprints`` opens a session from the app's sessionmaker and
    exits its context (releasing the connection) before returning.
    """
    from unittest.mock import AsyncMock, MagicMock

    from slowquery_demo.api.routers.dashboard import _poll_fingerprints

    session = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=cm)

    request = MagicMock()
    request.app.state.db_sessionmaker = factory

    async def _fake_list(sess: object) -> list[object]:
        assert sess is session
        return []

    import slowquery_demo.api.routers.dashboard as dash

    original = dash.repo.list_fingerprints
    dash.repo.list_fingerprints = _fake_list  # type: ignore[assignment]
    try:
        result = await _poll_fingerprints(request)
    finally:
        dash.repo.list_fingerprints = original  # type: ignore[assignment]

    assert result == []
    factory.assert_called_once()  # a fresh session was opened for this tick
    cm.__aenter__.assert_awaited_once()
    cm.__aexit__.assert_awaited_once()  # and released before returning
