"""S4: unit-lane tests for spec 09 (SSE stream endpoint).

Streaming tests hang under Starlette's sync TestClient because the
async generator never gets a clean close signal — known limitation.
Streaming behaviour is validated against the live Render URL (curl).

Unit-lane tests here cover the registration in the OpenAPI schema and
the shape of the generator's helper functions.
"""

from __future__ import annotations


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
