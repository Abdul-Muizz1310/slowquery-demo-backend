"""Final coverage tests for the last ~10 uncovered lines."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

# ── user_service.py:19 — happy path get_user ─────────────────────────────────


async def test_get_user_happy_path() -> None:
    """Cover user_service.py:19 — successful get_user returns DTO."""
    from slowquery_demo.services.user_service import get_user

    fake_user = MagicMock()
    fake_user.id = uuid.uuid4()
    fake_user.full_name = "Alice"
    fake_user.email = "alice@test.com"
    fake_user.created_at = datetime.now(UTC)

    session = AsyncMock()

    with patch(
        "slowquery_demo.services.user_service.user_repository.get_by_id",
        new=AsyncMock(return_value=fake_user),
    ):
        dto = await get_user(session, fake_user.id)
    assert dto.full_name == "Alice"


# ── services/store.py:93 — successful pool creation ─────────────────────────


async def test_store_writer_pool_success() -> None:
    """Cover store.py:93 — successful asyncpg pool creation returns pool."""
    from slowquery_demo.services.store import PostgresStoreWriter

    writer = PostgresStoreWriter.__new__(PostgresStoreWriter)
    writer._store_url = "postgresql://localhost/test"
    writer._pool = None
    writer._closed = False

    mock_pool = MagicMock()
    with patch(
        "slowquery_demo.services.store.asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)
    ):
        pool = await writer._ensure_pool()
    assert pool is mock_pool


# ── core/observability.py:242-243 — queue full after drop ────────────────────


def test_try_put_nowait_queue_full_then_empty() -> None:
    """Cover observability.py:242-243 — QueueFull/QueueEmpty caught silently."""
    from slowquery_demo.core.observability import _try_put_nowait

    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    # Fill the queue
    q.put_nowait({"dummy": True})
    # Calling _try_put_nowait on a full queue: it drops oldest then re-adds
    _try_put_nowait(q, {"new": True})
    assert q.qsize() == 1

    # Edge case: queue full AND get_nowait raises QueueEmpty somehow
    # (race condition). We mock to trigger the except branch.
    with patch.object(q, "get_nowait", side_effect=asyncio.QueueEmpty):
        with patch.object(q, "put_nowait", side_effect=asyncio.QueueFull):
            _try_put_nowait(q, {"overflow": True})  # should not raise


# ── core/branch_state.py:33 — returns valid branch name ─────────────────────


def test_load_branch_returns_fast(tmp_path, monkeypatch) -> None:
    """Cover branch_state.py:33 — returns cast BranchName for 'fast'."""
    from slowquery_demo.core.branch_state import load_branch

    state_file = tmp_path / ".branch_state"
    state_file.write_text("fast\n", encoding="utf-8")
    monkeypatch.setenv("BRANCH_STATE_FILE", str(state_file))

    result = load_branch()
    assert result == "fast"


# ── core/db_config.py:55 — kept.append in param filtering ───────────────────


def test_db_config_keeps_non_stripped_param() -> None:
    """Cover db_config.py:55 — non-stripped parameters are kept in output."""
    from slowquery_demo.core.db_config import normalise_asyncpg_url

    # 'application_name' is not in _STRIP_KEYS, so it hits line 55
    url = "postgresql://user:pass@host/db?application_name=myapp&sslmode=require"
    result = normalise_asyncpg_url(url)
    assert "application_name=myapp" in result
    assert "sslmode" not in result  # stripped
    assert "ssl=require" in result  # remapped
