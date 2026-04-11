"""S3 red: integration tests for spec 05 (install + dashboard router)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_slow_query_records_fingerprint(seeded_app, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 8."""
    import asyncio

    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine)
    seeded_app.get(f"/users/{sample_user_id}/orders")
    await asyncio.sleep(0.5)

    async with pg_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT fingerprint FROM query_fingerprints WHERE fingerprint LIKE '%orders%'")
            )
        ).first()
    assert row is not None
    assert "user_id" in row[0]


async def test_llm_called_once_on_rules_miss(seeded_app_llm, respx_mock) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 9."""
    from httpx import Response

    openrouter = respx_mock.route(host="openrouter.ai").mock(
        return_value=Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"diagnosis": "x", "suggestion": null, '
                            '"confidence": 0.1, "kind": "unknown"}'
                        }
                    }
                ]
            },
        )
    )
    # Synthetic plan path that rules don't match → LLM called.
    seeded_app_llm.post("/_slowquery/queries/abc123/force-explain")
    assert openrouter.call_count == 1


async def test_shutdown_closes_store_writer(test_client_lifespan) -> None:  # type: ignore[no-untyped-def]
    """Spec 05 test 10."""
    # TestClient(__exit__) triggers the shutdown event. The spy-patched
    # PostgresStoreWriter.close method should record exactly one call.
    with test_client_lifespan as client:
        client.get("/health")
    assert test_client_lifespan.store_close_calls == 1  # type: ignore[attr-defined]


async def _first_user(engine) -> str:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    async with engine.connect() as conn:
        return str(await conn.scalar(text("SELECT id FROM users LIMIT 1")))
