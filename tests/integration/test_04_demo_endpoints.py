"""S3 red: integration tests for spec 04 (demo endpoints x slowquery middleware)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_fingerprint_recorded_for_user_orders(seeded_app, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 8."""
    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine)
    seeded_app.get(f"/users/{sample_user_id}/orders")

    async with pg_engine.connect() as conn:
        count = await conn.scalar(text("SELECT COUNT(*) FROM query_fingerprints"))
    assert count >= 1


async def test_rule_fires_on_slow_branch(seeded_app_slow, pg_engine_slow) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 9."""
    import asyncio

    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine_slow)
    seeded_app_slow.get(f"/users/{sample_user_id}/orders")
    await asyncio.sleep(5)

    async with pg_engine_slow.connect() as conn:
        suggestions = await conn.scalar(text("SELECT COUNT(*) FROM suggestions"))
    assert suggestions >= 1


async def test_rule_does_not_fire_on_fast_branch(seeded_app_fast, pg_engine_fast) -> None:  # type: ignore[no-untyped-def]
    """Spec 04 test 10."""
    import asyncio

    from sqlalchemy import text

    sample_user_id = await _first_user(pg_engine_fast)
    seeded_app_fast.get(f"/users/{sample_user_id}/orders")
    await asyncio.sleep(5)

    async with pg_engine_fast.connect() as conn:
        suggestions = await conn.scalar(text("SELECT COUNT(*) FROM suggestions"))
    assert suggestions == 0


async def _first_user(engine) -> str:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    async with engine.connect() as conn:
        return str(await conn.scalar(text("SELECT id FROM users LIMIT 1")))
