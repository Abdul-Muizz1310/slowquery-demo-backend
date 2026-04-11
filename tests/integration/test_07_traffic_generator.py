"""S3 red: integration tests for spec 07 (traffic generator x live demo)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_run_produces_every_task_type(live_demo, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 4."""
    import asyncio
    import subprocess
    import sys

    result = await asyncio.to_thread(
        lambda: subprocess.run(
            [
                sys.executable,
                "scripts/traffic_generator.py",
                "--host",
                live_demo.base_url,
                "--duration",
                "10",
                "--users",
                "2",
                "--json",
            ],
            capture_output=True,
            timeout=60,
        )
    )
    assert result.returncode == 0
    out = result.stdout.decode()
    for path in (
        "/users/",
        "/users/{id}/orders",
        "/products",
        "/orders",
        "/orders/{id}",
        "/order_items",
    ):
        assert path in out or path.rstrip("/") in out


async def test_run_populates_fingerprints(live_demo, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 5."""
    from sqlalchemy import text

    await _run_generator(live_demo.base_url, duration=10)
    async with pg_engine.connect() as conn:
        count = await conn.scalar(text("SELECT COUNT(DISTINCT id) FROM query_fingerprints"))
    assert count >= 5


async def test_n_plus_one_burst_produces_dense_samples(live_demo, pg_engine) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 6."""
    from sqlalchemy import text

    await _run_generator(live_demo.base_url, duration=15)
    async with pg_engine.connect() as conn:
        burst = await conn.scalar(
            text(
                "SELECT MAX(c) FROM ("
                "  SELECT COUNT(*) c FROM query_samples"
                "  GROUP BY fingerprint_id, date_trunc('second', sampled_at)"
                ") x"
            )
        )
    assert burst >= 50


async def test_generator_runs_without_demo_mode(live_demo_non_demo) -> None:  # type: ignore[no-untyped-def]
    """Spec 07 test 7."""
    result = await _run_generator(live_demo_non_demo.base_url, duration=5)
    # It still runs to completion (though all requests likely 403); the
    # generator doesn't depend on demo mode being on.
    assert result is None or result.returncode == 0


async def _run_generator(host: str, duration: int = 10):  # type: ignore[no-untyped-def]
    import asyncio
    import subprocess
    import sys

    return await asyncio.to_thread(
        lambda: subprocess.run(
            [
                sys.executable,
                "scripts/traffic_generator.py",
                "--host",
                host,
                "--duration",
                str(duration),
                "--users",
                "2",
            ],
            capture_output=True,
            timeout=duration + 30,
        )
    )
