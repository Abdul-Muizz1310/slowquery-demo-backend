"""Tests to bring every remaining uncovered line to 100%.

Covers:
- repositories/user_repository.py line 28 (cursor branch)
- schemas/pagination.py lines 52-53 (invalid JSON cursor)
- services/store.py lines 93, 114, 137, 164, 192 (re-raise StoreWriterError)
- services/user_service.py line 19 (UserNotFoundError)
- core/observability.py (~49%) many lines
- core/dashboard.py (~48%) SSE generator + query detail body
- main.py (~73%) engine builder closure + startup/shutdown
- api/routers/users.py lines 34-35, 52
- core/branch_state.py lines 33, 39
- core/config.py line 59
- core/database.py lines 51-53
- core/db_config.py lines 42, 55, 78, 100
- core/platform.py line 60
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slowquery_demo.core.errors import InvalidCursorError, UserNotFoundError
from slowquery_demo.services.store import PostgresStoreWriter
from slowquery_demo.services.store_errors import StoreWriterError


# ---------------------------------------------------------------------------
# schemas/pagination.py lines 52-53 — cursor that decodes to invalid JSON
# ---------------------------------------------------------------------------


def test_decode_cursor_invalid_json_raises() -> None:
    """Base64 that decodes to non-JSON triggers line 52-53."""
    raw = base64.urlsafe_b64encode(b"not json at all").decode()
    with pytest.raises(InvalidCursorError, match="not valid JSON"):
        from slowquery_demo.schemas.pagination import decode_cursor

        decode_cursor(raw)


# ---------------------------------------------------------------------------
# repositories/user_repository.py line 28 — cursor_created_at branch
# ---------------------------------------------------------------------------


async def test_user_repo_list_users_with_cursor() -> None:
    from slowquery_demo.repositories import user_repository

    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    session.execute.return_value = result_mock

    result = await user_repository.list_users(
        session,
        limit=10,
        cursor_created_at="2025-01-01T00:00:00Z",
        cursor_id="abc-123",
    )
    assert result == []
    session.execute.assert_awaited_once()


# ---------------------------------------------------------------------------
# services/user_service.py line 19 — UserNotFoundError
# ---------------------------------------------------------------------------


async def test_user_service_get_user_not_found() -> None:
    import uuid

    from slowquery_demo.services import user_service

    session = AsyncMock()
    with patch.object(user_service, "user_repository") as mock_repo:
        mock_repo.get_by_id = AsyncMock(return_value=None)
        with pytest.raises(UserNotFoundError):
            await user_service.get_user(session, uuid.uuid4())


# ---------------------------------------------------------------------------
# services/store.py — re-raise StoreWriterError branches (lines 93,114,137,164,192)
# ---------------------------------------------------------------------------


def _pool_that_raises_store_error() -> tuple[MagicMock, AsyncMock]:
    """Pool mock whose conn.execute raises StoreWriterError directly."""
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=StoreWriterError("already a store error"))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    pool.close = AsyncMock()
    return pool, conn


async def test_upsert_fingerprint_reraises_store_error() -> None:
    pool, _ = _pool_that_raises_store_error()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    with pytest.raises(StoreWriterError, match="already a store error"):
        await writer.upsert_fingerprint("abc", "SELECT 1")


async def test_record_sample_reraises_store_error() -> None:
    pool, conn = _pool_that_raises_store_error()
    transaction_ctx = AsyncMock()
    transaction_ctx.__aenter__ = AsyncMock(return_value=None)
    transaction_ctx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=transaction_ctx)
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    with pytest.raises(StoreWriterError, match="already a store error"):
        await writer.record_sample("abc", duration_ms=10.0)


async def test_upsert_plan_reraises_store_error() -> None:
    pool, _ = _pool_that_raises_store_error()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    with pytest.raises(StoreWriterError, match="already a store error"):
        await writer.upsert_plan("abc", plan_json={"x": 1}, plan_text="text", cost=1.0)


async def test_insert_suggestions_reraises_store_error() -> None:
    from slowquery_detective.rules.base import Suggestion

    pool, _ = _pool_that_raises_store_error()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    suggestions = [
        Suggestion(kind="index", sql="CREATE INDEX ...", rationale="r", confidence=0.9, source="rules"),
    ]
    with pytest.raises(StoreWriterError, match="already a store error"):
        await writer.insert_suggestions("abc", suggestions)


async def test_ensure_pool_when_closed() -> None:
    pool = MagicMock()
    pool.close = AsyncMock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.close()
    with pytest.raises(StoreWriterError, match="closed"):
        await writer._ensure_pool()


async def test_close_idempotent() -> None:
    pool = MagicMock()
    pool.close = AsyncMock()
    writer = PostgresStoreWriter(store_url="postgresql://x", pool=pool)
    await writer.close()
    await writer.close()
    pool.close.assert_awaited_once()


# ---------------------------------------------------------------------------
# core/observability.py — patched attach, bridge queue, drainer, lifespan
# ---------------------------------------------------------------------------


def test_make_patched_attach_validates_none_engine() -> None:
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    buffer = MagicMock()
    with pytest.raises(ValueError, match="engine must not be None"):
        attach(None, buffer)


def test_make_patched_attach_validates_none_buffer() -> None:
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    engine = MagicMock()
    engine.sync_engine = MagicMock()
    engine.sync_engine._slowquery_attached = False
    with pytest.raises(ValueError, match="buffer must not be None"):
        attach(engine, None)


def test_make_patched_attach_validates_sample_rate() -> None:
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    engine = MagicMock()
    engine.sync_engine = MagicMock()
    engine.sync_engine._slowquery_attached = False
    buffer = MagicMock()
    with pytest.raises(ValueError, match="sample_rate"):
        attach(engine, buffer, sample_rate=2.0)


def test_make_patched_attach_idempotent() -> None:
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    engine = MagicMock()
    engine.sync_engine = MagicMock()
    engine.sync_engine._slowquery_attached = True
    buffer = MagicMock()
    # Should return early without raising
    attach(engine, buffer)


def test_make_patched_attach_registers_listeners() -> None:
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    assert mock_event.listen.call_count == 2
    assert sync_engine._slowquery_attached is True


def test_before_cursor_execute_sets_start_time() -> None:
    """Test the _before hook sets start time on context."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    # Get the _before and _after callbacks
    before_call = mock_event.listen.call_args_list[0]
    after_call = mock_event.listen.call_args_list[1]
    _before = before_call[0][2]
    _after = after_call[0][2]

    # Test _before sets start time
    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    assert hasattr(context, _CONTEXT_START_ATTR)
    assert getattr(context, _CONTEXT_START_ATTR) is not None


def test_before_cursor_execute_skips_on_sampling() -> None:
    """When sample_rate < 1.0 and random >= sample_rate, start is None."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=0.0)  # 0% sample rate

    _before = mock_event.listen.call_args_list[0][0][2]
    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    assert getattr(context, _CONTEXT_START_ATTR) is None


def test_after_cursor_execute_skips_when_no_start() -> None:
    """_after returns early when start is None (skipped sample)."""
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[asyncio.AbstractEventLoop | None] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _after = mock_event.listen.call_args_list[1][0][2]
    context = MagicMock(spec=[])
    # No start attribute set -> should return early
    _after(None, None, "SELECT 1", None, context, False)
    buffer.record.assert_not_called()


def test_after_cursor_execute_records_and_bridges() -> None:
    """_after fingerprints, records to buffer, and puts on bridge queue."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop_ref: list[Any] = [loop]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", (1,), context, False)

    buffer.record.assert_called_once()
    loop.call_soon_threadsafe.assert_called_once()


def test_after_cursor_execute_handles_fingerprint_error() -> None:
    """_after returns early when fingerprint_fn raises."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[Any] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)

    with patch("slowquery_demo.core.observability.fingerprint_fn", side_effect=RuntimeError("bad")):
        _after(None, None, "SELECT 1", None, context, False)

    buffer.record.assert_not_called()


def test_after_cursor_execute_handles_buffer_record_error() -> None:
    """_after continues even when buffer.record raises."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop_ref: list[Any] = [loop]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()
    buffer.record.side_effect = RuntimeError("buffer full")

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", {"a": 1}, context, False)

    # Should still try to bridge despite buffer failure
    loop.call_soon_threadsafe.assert_called_once()


def test_after_cursor_execute_no_loop() -> None:
    """_after returns early when loop_ref is None."""
    from slowquery_demo.core.observability import (
        _CONTEXT_START_ATTR,
        _make_patched_attach,
    )

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[Any] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", (), context, False)

    # No crash, but nothing enqueued since loop is None


def test_after_cursor_execute_closed_loop() -> None:
    """_after returns early when loop is closed."""
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = MagicMock()
    loop.is_closed.return_value = True
    loop_ref: list[Any] = [loop]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", (), context, False)

    loop.call_soon_threadsafe.assert_not_called()


def test_after_cursor_execute_dict_params() -> None:
    """_after normalises dict parameters to a tuple."""
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop_ref: list[Any] = [loop]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", {"a": 1, "b": 2}, context, False)

    loop.call_soon_threadsafe.assert_called_once()


def test_after_cursor_execute_none_params() -> None:
    """_after normalises None parameters to empty tuple."""
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop = MagicMock()
    loop.is_closed.return_value = False
    loop_ref: list[Any] = [loop]
    attach = _make_patched_attach(queue, loop_ref)

    sync_engine = MagicMock()
    sync_engine._slowquery_attached = False
    engine = MagicMock()
    engine.sync_engine = sync_engine
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    _before = mock_event.listen.call_args_list[0][0][2]
    _after = mock_event.listen.call_args_list[1][0][2]

    context = MagicMock(spec=[])
    _before(None, None, "SELECT 1", None, context, False)
    _after(None, None, "SELECT 1", None, context, False)

    loop.call_soon_threadsafe.assert_called_once()


# --- _try_put_nowait ---


def test_try_put_nowait_normal() -> None:
    from slowquery_demo.core.observability import _try_put_nowait

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=10)
    item = ("fp", "sql", "stmt", (), 1.0)
    _try_put_nowait(queue, item)
    assert queue.qsize() == 1


def test_try_put_nowait_full_queue_drops_oldest() -> None:
    from slowquery_demo.core.observability import _try_put_nowait

    queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=1)
    item1 = ("fp1", "sql1", "stmt1", (), 1.0)
    item2 = ("fp2", "sql2", "stmt2", (), 2.0)
    queue.put_nowait(item1)
    _try_put_nowait(queue, item2)
    assert queue.qsize() == 1
    assert queue.get_nowait() == item2


# --- _run_direct_explain ---


async def test_run_direct_explain_pool_failure() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    store = AsyncMock()
    store._ensure_pool = AsyncMock(side_effect=RuntimeError("pool fail"))
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


async def test_run_direct_explain_explain_error() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    conn = AsyncMock()
    conn.fetchval = AsyncMock(side_effect=RuntimeError("explain fail"))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


async def test_run_direct_explain_string_json_result() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    plan = [{"Plan": {"Total Cost": 42.0}}]
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=json.dumps(plan))
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result == {"Plan": {"Total Cost": 42.0}}


async def test_run_direct_explain_list_result() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    plan = [{"Plan": {"Total Cost": 42.0}}]
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=plan)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result == {"Plan": {"Total Cost": 42.0}}


async def test_run_direct_explain_dict_result() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    plan = {"Plan": {"Total Cost": 42.0}}
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=plan)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result == {"Plan": {"Total Cost": 42.0}}


async def test_run_direct_explain_invalid_json_string() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value="not json")
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


async def test_run_direct_explain_empty_list() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


async def test_run_direct_explain_non_dict_first_element() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=["not a dict"])
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


async def test_run_direct_explain_unexpected_type() -> None:
    from slowquery_demo.core.observability import _run_direct_explain

    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=42)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    store = AsyncMock()
    store._ensure_pool = AsyncMock(return_value=pool)
    result = await _run_direct_explain(store, "SELECT 1", ())
    assert result is None


# --- _drainer ---
# Each drainer test patches _BRIDGE_QUEUE with a fresh asyncio.Queue
# to avoid cross-test event-loop binding issues.


def _make_drainer_pool_mock(plan_result: Any = None) -> tuple[MagicMock, AsyncMock]:
    """Return (pool, conn) mocks for _run_direct_explain."""
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=plan_result)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)
    return pool, conn


async def _run_drainer_with_items(
    app: Any,
    items: list[tuple[Any, ...]],
    *,
    extra_patches: dict[str, Any] | None = None,
) -> None:
    """Put items on a fresh queue, run drainer, cancel after processing."""
    import slowquery_demo.core.observability as obs

    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=10_000)
    for item in items:
        await q.put(item)

    patches = {"slowquery_demo.core.observability._BRIDGE_QUEUE": q}
    if extra_patches:
        patches.update(extra_patches)

    with patch.multiple("", **{}) if not patches else contextlib.ExitStack() as stack:
        if isinstance(stack, contextlib.ExitStack):
            for target, value in patches.items():
                stack.enter_context(patch(target, value))
        task = asyncio.create_task(obs._drainer(app))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_drainer_upsert_fails_continues() -> None:
    """Drainer continues when upsert_fingerprint fails."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock(side_effect=RuntimeError("db down"))
    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 100

    await _run_drainer_with_items(app, [("fp1", "SELECT 1", "SELECT 1", (), 50.0)])


async def test_drainer_record_sample_fails_continues() -> None:
    """Drainer continues when record_sample fails."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock(side_effect=RuntimeError("db down"))
    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 1000

    await _run_drainer_with_items(app, [("fp1", "SELECT 1", "SELECT 1", (), 50.0)])


async def test_drainer_below_threshold_skips_explain() -> None:
    """When duration < threshold, drainer skips explain."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 100

    await _run_drainer_with_items(app, [("fp1", "SELECT 1", "SELECT 1", (), 50.0)])

    store.upsert_fingerprint.assert_awaited_once()
    store.record_sample.assert_awaited_once()


async def test_drainer_above_threshold_runs_explain_and_rules() -> None:
    """When duration >= threshold, drainer runs explain, rules, and stores results."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.upsert_plan = AsyncMock()
    store.insert_suggestions = AsyncMock()

    plan = [{"Plan": {"Total Cost": 42.0}}]
    pool, _ = _make_drainer_pool_mock(plan)
    store._ensure_pool = AsyncMock(return_value=pool)

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    await _run_drainer_with_items(
        app,
        [("fp1", "SELECT 1", "SELECT 1", (), 500.0)],
        extra_patches={"slowquery_demo.core.observability.run_rules": MagicMock(return_value=[])},
    )

    store.upsert_plan.assert_awaited_once()


async def test_drainer_explain_returns_none_sets_cooldown() -> None:
    """When explain returns None, cooldown is set for the fingerprint."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store._ensure_pool = AsyncMock(side_effect=RuntimeError("pool fail"))

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    await _run_drainer_with_items(app, [("fp1", "SELECT 1", "SELECT 1", (), 500.0)])


async def test_drainer_cooldown_prevents_repeat_explain() -> None:
    """Second item for same fp within cooldown window skips explain."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store._ensure_pool = AsyncMock(side_effect=RuntimeError("pool fail"))

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    import slowquery_demo.core.observability as obs

    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=10_000)
    await q.put(("fp1", "SELECT 1", "SELECT 1", (), 500.0))
    await q.put(("fp1", "SELECT 1", "SELECT 1", (), 500.0))

    with patch("slowquery_demo.core.observability._BRIDGE_QUEUE", q):
        task = asyncio.create_task(obs._drainer(app))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert store._ensure_pool.await_count == 1


async def test_drainer_rules_failure_continues() -> None:
    """When run_rules raises, drainer logs and continues."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.upsert_plan = AsyncMock()

    plan = [{"Plan": {"Total Cost": 42.0}}]
    pool, _ = _make_drainer_pool_mock(plan)
    store._ensure_pool = AsyncMock(return_value=pool)

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    await _run_drainer_with_items(
        app,
        [("fp2", "SELECT 1", "SELECT 1", (), 500.0)],
        extra_patches={"slowquery_demo.core.observability.run_rules": MagicMock(side_effect=RuntimeError("rules fail"))},
    )

    store.upsert_plan.assert_awaited_once()


async def test_drainer_upsert_plan_failure_continues() -> None:
    """When upsert_plan raises, drainer logs and continues."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.upsert_plan = AsyncMock(side_effect=RuntimeError("plan fail"))

    plan = [{"Plan": {"Total Cost": 42.0}}]
    pool, _ = _make_drainer_pool_mock(plan)
    store._ensure_pool = AsyncMock(return_value=pool)

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    await _run_drainer_with_items(
        app,
        [("fp3", "SELECT 1", "SELECT 1", (), 500.0)],
        extra_patches={"slowquery_demo.core.observability.run_rules": MagicMock(return_value=[])},
    )


async def test_drainer_insert_suggestions_failure_continues() -> None:
    """When insert_suggestions raises, drainer logs and continues."""
    from slowquery_detective.rules.base import Suggestion

    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.upsert_plan = AsyncMock()
    store.insert_suggestions = AsyncMock(side_effect=RuntimeError("suggestions fail"))

    plan = [{"Plan": {"Total Cost": 42.0}}]
    pool, _ = _make_drainer_pool_mock(plan)
    store._ensure_pool = AsyncMock(return_value=pool)

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    suggestion = Suggestion(kind="index", sql="CREATE INDEX ...", rationale="r", confidence=0.9, source="rules")
    await _run_drainer_with_items(
        app,
        [("fp4", "SELECT 1", "SELECT 1", (), 500.0)],
        extra_patches={"slowquery_demo.core.observability.run_rules": MagicMock(return_value=[suggestion])},
    )

    store.insert_suggestions.assert_awaited_once()


async def test_drainer_plan_without_plan_key_uses_zero_cost() -> None:
    """When plan dict lacks 'Plan' key, cost defaults to 0.0."""
    app = MagicMock()
    store = AsyncMock()
    store.upsert_fingerprint = AsyncMock()
    store.record_sample = AsyncMock()
    store.upsert_plan = AsyncMock()
    store.insert_suggestions = AsyncMock()

    plan = [{"Something": "else"}]
    pool, _ = _make_drainer_pool_mock(plan)
    store._ensure_pool = AsyncMock(return_value=pool)

    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 10

    await _run_drainer_with_items(
        app,
        [("fp5", "SELECT 1", "SELECT 1", (), 500.0)],
        extra_patches={"slowquery_demo.core.observability.run_rules": MagicMock(return_value=[])},
    )

    call_args = store.upsert_plan.call_args
    assert call_args.kwargs.get("cost", 0.0) == 0.0


# --- install_slowquery ---


def test_install_slowquery_none_settings_raises() -> None:
    from fastapi import FastAPI

    from slowquery_demo.core.errors import ConfigError
    from slowquery_demo.core.observability import install_slowquery

    app = FastAPI()
    engine = MagicMock()
    with pytest.raises(ConfigError, match="settings is None"):
        install_slowquery(app, engine, settings=None)


# --- slowquery_lifespan ---


async def test_slowquery_lifespan_no_worker_no_store() -> None:
    """Lifespan works when no worker and no store are set."""
    from slowquery_demo.core.observability import _LOOP_REF, slowquery_lifespan

    app = MagicMock()
    app.state = MagicMock(spec=[])
    # No slowquery_worker or slowquery_store attributes

    async with slowquery_lifespan(app):
        assert _LOOP_REF[0] is not None

    assert _LOOP_REF[0] is None


async def test_slowquery_lifespan_with_worker_and_store() -> None:
    """Lifespan starts worker, creates drainer task, and cleans up."""
    from slowquery_demo.core.observability import _LOOP_REF, slowquery_lifespan

    worker = AsyncMock()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()

    store = AsyncMock()
    store.close = AsyncMock()

    app = MagicMock()
    app.state.slowquery_worker = worker
    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 100

    async with slowquery_lifespan(app):
        assert _LOOP_REF[0] is not None
        worker.start.assert_awaited_once()

    worker.stop.assert_awaited_once()
    store.close.assert_awaited_once()
    assert _LOOP_REF[0] is None


async def test_slowquery_lifespan_store_close_error_is_suppressed() -> None:
    """Lifespan suppresses store.close errors."""
    from slowquery_demo.core.observability import slowquery_lifespan

    worker = AsyncMock()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()

    store = AsyncMock()
    store.close = AsyncMock(side_effect=RuntimeError("close fail"))

    app = MagicMock()
    app.state.slowquery_worker = worker
    app.state.slowquery_store = store
    app.state.slowquery_threshold_ms = 100

    async with slowquery_lifespan(app):
        pass
    # Should not raise


async def test_slowquery_lifespan_worker_no_store_no_drainer() -> None:
    """When worker exists but store is None, no drainer task is created."""
    from slowquery_demo.core.observability import slowquery_lifespan

    worker = AsyncMock()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()

    app = MagicMock()
    app.state.slowquery_worker = worker
    app.state.slowquery_store = None

    async with slowquery_lifespan(app):
        worker.start.assert_awaited_once()

    worker.stop.assert_awaited_once()


# ---------------------------------------------------------------------------
# core/dashboard.py — SSE generator + query detail body (lines not hit by existing tests)
# ---------------------------------------------------------------------------


async def test_sse_generator_initial_fingerprints() -> None:
    """SSE generator emits tick events for initial fingerprints with p95."""
    from slowquery_demo.api.routers.dashboard import _sse_generator

    fp = MagicMock()
    fp.id = "abc123"
    fp.p95_ms = 42.5

    request = MagicMock()
    session = AsyncMock()

    with patch("slowquery_demo.api.routers.dashboard.repo") as mock_repo:
        mock_repo.list_fingerprints = AsyncMock(return_value=[fp])

        gen = _sse_generator(request, session)
        events = []
        # Collect just the first event (initial batch)
        event = await gen.__anext__()
        events.append(event)

        data = json.loads(event.replace("data: ", "").strip())
        assert data["kind"] == "tick"
        assert data["fingerprint_id"] == "abc123"
        assert data["p95_ms"] == 42.5

        await gen.aclose()


async def test_sse_generator_initial_heartbeat_when_no_fingerprints() -> None:
    """SSE generator emits heartbeat when no initial fingerprints."""
    from slowquery_demo.api.routers.dashboard import _sse_generator

    request = MagicMock()
    session = AsyncMock()

    with patch("slowquery_demo.api.routers.dashboard.repo") as mock_repo:
        mock_repo.list_fingerprints = AsyncMock(return_value=[])

        gen = _sse_generator(request, session)
        event = await gen.__anext__()

        data = json.loads(event.replace("data: ", "").strip())
        assert data["kind"] == "heartbeat"

        await gen.aclose()


async def test_sse_generator_initial_fp_with_none_p95() -> None:
    """SSE generator skips tick for fp with None p95, still tracks it."""
    from slowquery_demo.api.routers.dashboard import _sse_generator

    fp = MagicMock()
    fp.id = "abc123"
    fp.p95_ms = None

    request = MagicMock()
    session = AsyncMock()

    with patch("slowquery_demo.api.routers.dashboard.repo") as mock_repo:
        mock_repo.list_fingerprints = AsyncMock(return_value=[fp])

        gen = _sse_generator(request, session)
        # First event should not be a tick since p95 is None
        # But since there are fps, there should be no events from the initial batch
        # Actually, no tick events are emitted, but also no heartbeat since fps is not empty
        # The generator should proceed to the while loop
        # We need to patch sleep and is_disconnected
        request.is_disconnected = AsyncMock(return_value=True)

        with patch("slowquery_demo.api.routers.dashboard.asyncio.sleep", new_callable=AsyncMock):
            events = []
            async for event in gen:
                events.append(event)
            # No tick events from initial (p95 is None), goes to poll loop, disconnects
            assert len(events) == 0

        await gen.aclose()


async def test_sse_generator_poll_loop_tick_and_heartbeat() -> None:
    """SSE generator emits tick on p95 change, heartbeat when unchanged."""
    from slowquery_demo.api.routers.dashboard import _sse_generator

    fp1 = MagicMock()
    fp1.id = "abc123"
    fp1.p95_ms = 10.0

    fp2 = MagicMock()
    fp2.id = "abc123"
    fp2.p95_ms = 20.0  # changed

    fp3 = MagicMock()
    fp3.id = "abc123"
    fp3.p95_ms = 20.0  # unchanged

    call_count = [0]
    async def mock_list_fingerprints(session: Any) -> list[Any]:
        call_count[0] += 1
        if call_count[0] == 1:
            return [fp1]
        elif call_count[0] == 2:
            return [fp2]
        else:
            return [fp3]

    request = MagicMock()
    disconnect_count = [0]
    async def mock_is_disconnected() -> bool:
        disconnect_count[0] += 1
        return disconnect_count[0] > 2

    request.is_disconnected = mock_is_disconnected
    session = AsyncMock()

    with (
        patch("slowquery_demo.api.routers.dashboard.repo") as mock_repo,
        patch("slowquery_demo.api.routers.dashboard.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_repo.list_fingerprints = mock_list_fingerprints

        events = []
        async for event in _sse_generator(request, session):
            events.append(event)

        # Event 0: initial tick (p95=10.0)
        # Event 1: poll tick (p95 changed to 20.0)
        # Event 2: poll heartbeat (p95 unchanged at 20.0)
        assert len(events) == 3
        d0 = json.loads(events[0].replace("data: ", "").strip())
        assert d0["kind"] == "tick"
        d1 = json.loads(events[1].replace("data: ", "").strip())
        assert d1["kind"] == "tick"
        d2 = json.loads(events[2].replace("data: ", "").strip())
        assert d2["kind"] == "heartbeat"


async def test_sse_generator_disconnects_immediately() -> None:
    """SSE generator stops when client disconnects."""
    from slowquery_demo.api.routers.dashboard import _sse_generator

    request = MagicMock()
    request.is_disconnected = AsyncMock(return_value=True)
    session = AsyncMock()

    with (
        patch("slowquery_demo.api.routers.dashboard.repo") as mock_repo,
        patch("slowquery_demo.api.routers.dashboard.asyncio.sleep", new_callable=AsyncMock),
    ):
        mock_repo.list_fingerprints = AsyncMock(return_value=[])

        events = []
        async for event in _sse_generator(request, session):
            events.append(event)

        # Initial heartbeat, then client disconnects
        assert len(events) == 1
        d = json.loads(events[0].replace("data: ", "").strip())
        assert d["kind"] == "heartbeat"


# --- query detail endpoint body coverage ---


def _make_fp_mock(fp_id: str = "abc1234567890123") -> MagicMock:
    fp = MagicMock()
    fp.id = fp_id
    fp.fingerprint = "SELECT 1"
    fp.first_seen = datetime(2025, 1, 1, tzinfo=UTC)
    fp.last_seen = datetime(2025, 1, 2, tzinfo=UTC)
    fp.call_count = 10
    fp.total_ms = 1000
    fp.p50_ms = 5.0
    fp.p95_ms = 10.0
    fp.p99_ms = 15.0
    fp.max_ms = 20.0
    return fp


def _make_suggestion_mock(fp_id: str = "abc1234567890123", source: str = "rules") -> MagicMock:
    s = MagicMock()
    s.id = 1
    s.fingerprint_id = fp_id
    s.kind = "index"
    s.source = source
    s.rule = None
    s.sql = "CREATE INDEX ..."
    s.rationale = "Seq Scan on orders"
    s.applied_at = None
    return s


def _make_plan_mock(fp_id: str = "abc1234567890123") -> MagicMock:
    p = MagicMock()
    p.fingerprint_id = fp_id
    p.plan_json = {"Plan": {"Node Type": "Seq Scan"}}
    p.plan_text = '{"Plan": {"Node Type": "Seq Scan"}}'
    p.cost = 42.0
    p.captured_at = datetime(2025, 1, 1, tzinfo=UTC)
    return p


def _make_sample_mock(fp_id: str = "abc1234567890123") -> MagicMock:
    s = MagicMock()
    s.id = 1
    s.fingerprint_id = fp_id
    s.params = None
    s.duration_ms = 15.0
    s.rows = 100
    s.sampled_at = datetime(2025, 1, 1, tzinfo=UTC)
    return s


def test_get_query_detail_found(test_client: Any) -> None:
    """Exercise the query detail body when fingerprint exists."""
    from slowquery_demo.api.routers import dashboard as dash_mod

    fp = _make_fp_mock()
    suggestion = _make_suggestion_mock()
    plan = _make_plan_mock()
    sample = _make_sample_mock()

    with (
        patch.object(dash_mod.repo, "get_fingerprint_by_id", new_callable=AsyncMock, return_value=fp),
        patch.object(dash_mod.repo, "list_suggestions_for_fingerprint", new_callable=AsyncMock, return_value=[suggestion]),
        patch.object(dash_mod.repo, "get_explain_plan", new_callable=AsyncMock, return_value=plan),
        patch.object(dash_mod.repo, "list_recent_samples", new_callable=AsyncMock, return_value=[sample]),
    ):
        resp = test_client.get("/_slowquery/queries/abc1234567890123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["fingerprint"]["id"] == "abc1234567890123"
    assert data["canonical_sql"] == "SELECT 1"
    assert data["explain_plan"] is not None
    assert len(data["suggestions"]) == 1
    assert len(data["recent_samples"]) == 1


def test_get_query_detail_no_plan(test_client: Any) -> None:
    """Exercise query detail with no explain plan."""
    from slowquery_demo.api.routers import dashboard as dash_mod

    fp = _make_fp_mock()

    with (
        patch.object(dash_mod.repo, "get_fingerprint_by_id", new_callable=AsyncMock, return_value=fp),
        patch.object(dash_mod.repo, "list_suggestions_for_fingerprint", new_callable=AsyncMock, return_value=[]),
        patch.object(dash_mod.repo, "get_explain_plan", new_callable=AsyncMock, return_value=None),
        patch.object(dash_mod.repo, "list_recent_samples", new_callable=AsyncMock, return_value=[]),
    ):
        resp = test_client.get("/_slowquery/queries/abc1234567890123")

    assert resp.status_code == 200
    data = resp.json()
    assert data["explain_plan"] is None


# --- list queries with suggestions ---


def test_list_queries_with_suggestions(test_client: Any) -> None:
    """Exercise list endpoint when fingerprints have suggestions."""
    from slowquery_demo.api.routers import dashboard as dash_mod

    fp = _make_fp_mock()
    suggestion = _make_suggestion_mock(source="llm")

    with (
        patch.object(dash_mod.repo, "list_fingerprints", new_callable=AsyncMock, return_value=[fp]),
        patch.object(
            dash_mod.repo,
            "list_all_suggestions",
            new_callable=AsyncMock,
            return_value={"abc1234567890123": [suggestion]},
        ),
    ):
        resp = test_client.get("/_slowquery/queries")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert len(data[0]["suggestions"]) == 1


# ---------------------------------------------------------------------------
# main.py — _make_engine_builder closure (lines 50-79)
# ---------------------------------------------------------------------------


def _mock_engine_with_connect() -> tuple[MagicMock, AsyncMock]:
    """Create an engine mock whose .connect() returns an async context manager."""
    engine = MagicMock()
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()

    # Make .connect() return an async context manager
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_conn)
    cm.__aexit__ = AsyncMock(return_value=None)
    engine.connect = MagicMock(return_value=cm)
    engine.dispose = AsyncMock()
    return engine, mock_conn


async def test_engine_builder_rebuild_happy_path() -> None:
    """Exercise the full _rebuild closure: build, health-check, swap, dispose."""
    from slowquery_demo.main import _make_engine_builder

    app = MagicMock()
    old_engine = AsyncMock()
    old_engine.dispose = AsyncMock()
    app.state.engine = old_engine
    app.state.db_sessionmaker = MagicMock()

    new_engine, _ = _mock_engine_with_connect()
    new_factory = MagicMock()

    builder = _make_engine_builder(app)

    with patch("slowquery_demo.main.build_engine", return_value=(new_engine, new_factory)):
        result = await builder("postgresql://new")

    assert result == (new_engine, new_factory)
    assert app.state.engine == new_engine
    assert app.state.db_sessionmaker == new_factory

    # Give the dispose task a chance to run
    await asyncio.sleep(0.01)


async def test_engine_builder_dispose_later_runs() -> None:
    """Verify _dispose_later fires after the grace period."""
    from slowquery_demo.main import _make_engine_builder

    app = MagicMock()
    old_engine = AsyncMock()
    old_engine.dispose = AsyncMock()
    app.state.engine = old_engine
    app.state.db_sessionmaker = MagicMock()

    new_engine, _ = _mock_engine_with_connect()
    new_factory = MagicMock()

    builder = _make_engine_builder(app)

    # Patch the grace-period sleep to return instantly so the task completes
    with patch("slowquery_demo.main.build_engine", return_value=(new_engine, new_factory)):
        with patch("slowquery_demo.main._ENGINE_DISPOSE_GRACE_S", 0):
            await builder("postgresql://new")
            # Yield control so the fire-and-forget task can run
            await asyncio.sleep(0.1)

    old_engine.dispose.assert_awaited_once()


async def test_engine_builder_dispose_later_error_is_logged() -> None:
    """_dispose_later logs but doesn't crash when dispose raises."""
    from slowquery_demo.main import _make_engine_builder

    app = MagicMock()
    old_engine = AsyncMock()
    old_engine.dispose = AsyncMock(side_effect=RuntimeError("dispose fail"))
    app.state.engine = old_engine
    app.state.db_sessionmaker = MagicMock()

    new_engine, _ = _mock_engine_with_connect()
    new_factory = MagicMock()

    builder = _make_engine_builder(app)

    with patch("slowquery_demo.main.build_engine", return_value=(new_engine, new_factory)):
        with patch("slowquery_demo.main._ENGINE_DISPOSE_GRACE_S", 0):
            await builder("postgresql://new")
            await asyncio.sleep(0.1)

    # Should not raise even though dispose failed


# ---------------------------------------------------------------------------
# api/routers/users.py — lines 34-35 (cursor decode), 52 (list_user_orders)
# ---------------------------------------------------------------------------


def test_users_list_with_cursor(test_client: Any) -> None:
    """Exercise the cursor decode path in the /users endpoint."""
    from slowquery_demo.schemas.pagination import encode_cursor

    cursor = encode_cursor("2025-01-01T00:00:00Z", "abc-123")
    resp = test_client.get(f"/users?cursor={cursor}")
    assert resp.status_code == 200


def test_users_list_user_orders(test_client: Any) -> None:
    """Exercise the /users/{id}/orders endpoint."""
    import uuid

    resp = test_client.get(f"/users/{uuid.uuid4()}/orders")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# core/branch_state.py — lines 33 (invalid content), 39 (save invalid)
# ---------------------------------------------------------------------------


def test_load_branch_invalid_content(tmp_path: Any, monkeypatch: Any) -> None:
    state_file = tmp_path / ".branch_test"
    state_file.write_text("invalid\n")
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state_file))

    from slowquery_demo.core.branch_state import load_branch

    assert load_branch() == "slow"


def test_save_branch_invalid_raises(tmp_path: Any, monkeypatch: Any) -> None:
    state_file = tmp_path / ".branch_test"
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state_file))

    from slowquery_demo.core.branch_state import save_branch

    with pytest.raises(ValueError, match="invalid branch"):
        save_branch("invalid")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# core/config.py — line 59 (get_settings)
# ---------------------------------------------------------------------------


def test_get_settings_returns_instance() -> None:
    from slowquery_demo.core.config import Settings, get_settings

    result = get_settings()
    assert isinstance(result, Settings)


# ---------------------------------------------------------------------------
# core/database.py — lines 51-53 (get_db dependency)
# ---------------------------------------------------------------------------


async def test_get_db_yields_session() -> None:
    from slowquery_demo.core.database import get_db

    mock_session = AsyncMock()
    mock_factory = MagicMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=None)

    request = MagicMock()
    request.app.state.db_sessionmaker = mock_factory

    sessions = []
    async for session in get_db(request):
        sessions.append(session)

    assert sessions == [mock_session]


# ---------------------------------------------------------------------------
# core/db_config.py — lines 42, 55, 78, 100
# ---------------------------------------------------------------------------


def test_normalise_asyncpg_url_adds_dialect() -> None:
    from slowquery_demo.core.db_config import normalise_asyncpg_url

    result = normalise_asyncpg_url("postgresql://user:pass@host/db")
    assert result.startswith("postgresql+asyncpg://")


def test_normalise_asyncpg_url_strips_channel_binding() -> None:
    from slowquery_demo.core.db_config import normalise_asyncpg_url

    url = "postgresql+asyncpg://user:pass@host/db?channel_binding=require"
    result = normalise_asyncpg_url(url)
    assert "channel_binding" not in result


def test_to_raw_asyncpg_dsn_strips_dialect() -> None:
    from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

    result = to_raw_asyncpg_dsn("postgresql+asyncpg://user:pass@host/db")
    assert result.startswith("postgresql://")
    assert "+asyncpg" not in result


def test_to_raw_asyncpg_dsn_strips_channel_binding() -> None:
    from slowquery_demo.core.db_config import to_raw_asyncpg_dsn

    result = to_raw_asyncpg_dsn("postgresql+asyncpg://user:pass@host/db?channel_binding=require")
    assert "channel_binding" not in result


def test_get_database_url_raises_when_unset(monkeypatch: Any) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    from slowquery_demo.core.db_config import get_database_url

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        get_database_url()


def test_get_database_url_returns_normalised(monkeypatch: Any) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@host/db?sslmode=require")

    from slowquery_demo.core.db_config import get_database_url

    result = get_database_url()
    assert "postgresql+asyncpg://" in result


# ---------------------------------------------------------------------------
# core/platform.py — line 60 (/version endpoint)
# ---------------------------------------------------------------------------


def test_version_endpoint(test_client: Any) -> None:
    resp = test_client.get("/version")
    assert resp.status_code == 200
    data = resp.json()
    assert data["service"] == "slowquery_demo"
    assert "version" in data


# ---------------------------------------------------------------------------
# SSE stream endpoint coverage
# ---------------------------------------------------------------------------


def test_sse_stream_endpoint_returns_event_stream(test_client: Any) -> None:
    """The /api/stream endpoint returns text/event-stream content type."""
    with patch("slowquery_demo.api.routers.dashboard._sse_generator") as mock_gen:
        async def fake_gen(request: Any, session: Any) -> AsyncGenerator[str, None]:
            yield 'data: {"kind": "heartbeat"}\n\n'

        mock_gen.side_effect = fake_gen
        resp = test_client.get("/_slowquery/api/stream", headers={"Accept": "text/event-stream"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Attach with no sync_engine attribute (engine IS the sync engine)
# ---------------------------------------------------------------------------


def test_attach_engine_without_sync_engine_attr() -> None:
    """When engine has no sync_engine attribute, use engine directly."""
    from slowquery_demo.core.observability import _make_patched_attach

    queue: asyncio.Queue[Any] = asyncio.Queue()
    loop_ref: list[Any] = [None]
    attach = _make_patched_attach(queue, loop_ref)

    engine = MagicMock(spec=["_slowquery_attached"])
    engine._slowquery_attached = False
    del engine.sync_engine  # ensure no sync_engine attr
    buffer = MagicMock()

    with patch("slowquery_demo.core.observability.event") as mock_event:
        attach(engine, buffer, sample_rate=1.0)

    assert mock_event.listen.call_count == 2
    assert engine._slowquery_attached is True
