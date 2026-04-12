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
