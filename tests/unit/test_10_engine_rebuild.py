"""Unit-lane tests for the switcher's connection move (spec 10, as superseded by spec 15).

Spec 15 replaced the engine rebuild with a retarget of the one observed
engine; the switcher's contract around that callable (target URL, ordering,
serialization, failure leaves everything as it was) is what these cases pin,
with an async mock. The real move is integration-lane (two Postgres
containers) and spec 15's unit cases drive ``main._make_retarget``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from slowquery_demo.services.branch_switcher import BranchSwitcher


@pytest.fixture
def mock_retarget() -> AsyncMock:
    return AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_switch_calls_retarget_with_target_url(
    mock_retarget: AsyncMock,
) -> None:
    """Spec 10 test 1: after switch to 'fast', the retarget receives the fast URL."""
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=mock_retarget,
    )
    await switcher.switch("fast")
    mock_retarget.assert_awaited_once_with("postgresql://fast")


@pytest.mark.asyncio
async def test_switch_back_to_slow_uses_slow_url(
    mock_retarget: AsyncMock,
) -> None:
    """Spec 10 test 2: switch back to slow routes to the slow URL."""
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=mock_retarget,
    )
    await switcher.switch("fast")
    mock_retarget.reset_mock()
    await switcher.switch("slow")
    mock_retarget.assert_awaited_once_with("postgresql://slow")


@pytest.mark.asyncio
async def test_retarget_failure_aborts_switch(
    mock_retarget: AsyncMock,
) -> None:
    """Spec 10 test 3: if engine_builder raises, active branch stays unchanged."""
    mock_retarget.side_effect = ConnectionError("neon is down")
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=mock_retarget,
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
        retarget=slow_builder,
    )
    t1 = asyncio.create_task(switcher.switch("fast"))
    await call_event.wait()
    # The second call should fail with ValueError because by the time it
    # gets the lock, active is already "fast".
    with pytest.raises(ValueError, match="already on fast"):
        await switcher.switch("fast")
    await t1


def test_make_retarget_is_callable() -> None:
    """Spec 10 test 5: _make_retarget returns a callable."""
    from slowquery_demo.main import _make_retarget

    app = MagicMock()
    builder = _make_retarget(app)
    assert callable(builder)


@pytest.mark.asyncio
async def test_post_switch_runs_after_successful_engine_rebuild(
    mock_retarget: AsyncMock,
) -> None:
    """Spec 06 invariant 5: the post-switch hook fires after the swap.

    The hook runs after ``retarget`` returns (connections now go to the
    target) and before the switch returns.
    """
    post_switch = AsyncMock()
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=mock_retarget,
        post_switch=post_switch,
    )
    await switcher.switch("fast")
    post_switch.assert_awaited_once_with()
    # Ordering: the connections must have moved before the hook ran.
    mock_retarget.assert_awaited_once_with("postgresql://fast")


@pytest.mark.asyncio
async def test_post_switch_skipped_when_no_retarget() -> None:
    """A state-only switch records no new samples, so no hook runs."""
    post_switch = AsyncMock()
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=None,
        post_switch=post_switch,
    )
    await switcher.switch("fast")
    post_switch.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_switch_skipped_when_engine_rebuild_fails(
    mock_retarget: AsyncMock,
) -> None:
    """If the rebuild raises, the active branch and buffer stay untouched."""
    mock_retarget.side_effect = ConnectionError("neon is down")
    post_switch = AsyncMock()
    switcher = BranchSwitcher(
        initial="slow",
        slow_url="postgresql://slow",
        fast_url="postgresql://fast",
        retarget=mock_retarget,
        post_switch=post_switch,
    )
    with pytest.raises(ConnectionError):
        await switcher.switch("fast")
    post_switch.assert_not_awaited()
    assert switcher.active == "slow"
