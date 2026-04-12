"""S4: unit-lane tests for spec 10 (engine rebuild on branch switch).

These tests verify the BranchSwitcher integration with the engine_builder
callable, using an async mock. Real Postgres engine rebuild is integration-
lane (Testcontainers with two databases).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from slowquery_demo.services.branch_switcher import BranchSwitcher


@pytest.fixture
def mock_engine_builder() -> AsyncMock:
    builder = AsyncMock()
    builder.return_value = (MagicMock(), MagicMock())
    return builder


@pytest.mark.asyncio
async def test_switch_calls_engine_builder_with_target_url(
    mock_engine_builder: AsyncMock,
) -> None:
    """Spec 10 test 1: after switch to 'fast', the engine_builder receives the fast URL."""
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        engine_builder=mock_engine_builder,
    )
    await switcher.switch("fast")
    mock_engine_builder.assert_awaited_once_with("postgresql://fast")


@pytest.mark.asyncio
async def test_switch_back_to_slow_uses_slow_url(
    mock_engine_builder: AsyncMock,
) -> None:
    """Spec 10 test 2: switch back to slow routes to the slow URL."""
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        engine_builder=mock_engine_builder,
    )
    await switcher.switch("fast")
    mock_engine_builder.reset_mock()
    await switcher.switch("slow")
    mock_engine_builder.assert_awaited_once_with("postgresql://slow")


@pytest.mark.asyncio
async def test_engine_builder_failure_aborts_switch(
    mock_engine_builder: AsyncMock,
) -> None:
    """Spec 10 test 3: if engine_builder raises, active branch stays unchanged."""
    mock_engine_builder.side_effect = ConnectionError("neon is down")
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        engine_builder=mock_engine_builder,
    )
    with pytest.raises(ConnectionError):
        await switcher.switch("fast")
    assert switcher.active == "slow"


@pytest.mark.asyncio
async def test_concurrent_switches_serialized() -> None:
    """Spec 10 test 4: two concurrent switch calls are serialized by the lock."""
    call_order: list[str] = []
    call_event = asyncio.Event()

    async def slow_builder(url: str) -> tuple[None, None]:
        call_order.append(f"start-{url}")
        if not call_event.is_set():
            call_event.set()
            await asyncio.sleep(0.1)
        call_order.append(f"end-{url}")
        return (None, None)

    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        engine_builder=slow_builder,
    )
    t1 = asyncio.create_task(switcher.switch("fast"))
    await call_event.wait()
    # The second call should fail with ValueError because by the time it
    # gets the lock, active is already "fast".
    with pytest.raises(ValueError, match="already on fast"):
        await switcher.switch("fast")
    await t1


def test_make_engine_builder_is_callable() -> None:
    """Spec 10 test 5: _make_engine_builder returns a callable."""
    from slowquery_demo.main import _make_engine_builder

    app = MagicMock()
    builder = _make_engine_builder(app)
    assert callable(builder)
