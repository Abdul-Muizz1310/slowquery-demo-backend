"""Unit tests for repository layer coverage.

Exercises all repository functions with mock AsyncSession.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock


def _mock_session_scalar_one_or_none(value: object) -> AsyncMock:
    """Build a mock session whose execute returns a result with scalar_one_or_none."""
    session = AsyncMock()
    result = MagicMock()  # Sync mock — result is not awaited
    result.scalar_one_or_none.return_value = value
    session.execute.return_value = result
    return session


def _mock_session_scalars_all(values: list[object]) -> AsyncMock:
    """Build a mock session whose execute returns result.scalars().all()."""
    session = AsyncMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = values
    result = MagicMock()
    result.scalars.return_value = scalars_mock
    session.execute.return_value = result
    return session


# ---------------------------------------------------------------------------
# order_repository
# ---------------------------------------------------------------------------


async def test_order_get_by_id() -> None:
    from slowquery_demo.repositories import order_repository

    mock_order = MagicMock()
    session = _mock_session_scalar_one_or_none(mock_order)

    result = await order_repository.get_by_id(session, uuid.uuid4())
    assert result is mock_order
    session.execute.assert_awaited_once()


async def test_order_list_recent() -> None:
    from slowquery_demo.repositories import order_repository

    mock_order = MagicMock()
    session = _mock_session_scalars_all([mock_order])

    result = await order_repository.list_recent(session, limit=10)
    assert result == [mock_order]


async def test_order_list_for_user() -> None:
    from slowquery_demo.repositories import order_repository

    mock_order = MagicMock()
    session = _mock_session_scalars_all([mock_order])

    result = await order_repository.list_for_user(session, uuid.uuid4(), limit=10)
    assert result == [mock_order]


# ---------------------------------------------------------------------------
# product_repository
# ---------------------------------------------------------------------------


async def test_product_get_by_id() -> None:
    from slowquery_demo.repositories import product_repository

    mock_product = MagicMock()
    session = _mock_session_scalar_one_or_none(mock_product)

    result = await product_repository.get_by_id(session, uuid.uuid4())
    assert result is mock_product


async def test_product_list_products() -> None:
    from slowquery_demo.repositories import product_repository

    mock_product = MagicMock()
    session = _mock_session_scalars_all([mock_product])

    result = await product_repository.list_products(session, limit=10)
    assert result == [mock_product]


# ---------------------------------------------------------------------------
# order_item_repository
# ---------------------------------------------------------------------------


async def test_order_item_list_for_order() -> None:
    from slowquery_demo.repositories import order_item_repository

    mock_item = MagicMock()
    session = _mock_session_scalars_all([mock_item])

    result = await order_item_repository.list_for_order(session, uuid.uuid4())
    assert result == [mock_item]


async def test_order_item_list_for_product() -> None:
    from slowquery_demo.repositories import order_item_repository

    mock_item = MagicMock()
    session = _mock_session_scalars_all([mock_item])

    result = await order_item_repository.list_for_product(session, uuid.uuid4(), limit=10)
    assert result == [mock_item]


# ---------------------------------------------------------------------------
# slowquery_repository
# ---------------------------------------------------------------------------


async def test_slowquery_list_fingerprints() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_fp = MagicMock()
    session = _mock_session_scalars_all([mock_fp])

    result = await slowquery_repository.list_fingerprints(session)
    assert result == [mock_fp]


async def test_slowquery_get_fingerprint_by_id() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_fp = MagicMock()
    session = _mock_session_scalar_one_or_none(mock_fp)

    result = await slowquery_repository.get_fingerprint_by_id(session, "abc123")
    assert result is mock_fp


async def test_slowquery_list_suggestions_for_fingerprint() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_suggestion = MagicMock()
    session = _mock_session_scalars_all([mock_suggestion])

    result = await slowquery_repository.list_suggestions_for_fingerprint(session, "abc123")
    assert result == [mock_suggestion]


async def test_slowquery_get_explain_plan() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_plan = MagicMock()
    session = _mock_session_scalar_one_or_none(mock_plan)

    result = await slowquery_repository.get_explain_plan(session, "abc123")
    assert result is mock_plan


async def test_slowquery_list_recent_samples() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_sample = MagicMock()
    session = _mock_session_scalars_all([mock_sample])

    result = await slowquery_repository.list_recent_samples(session, "abc123")
    assert result == [mock_sample]


async def test_slowquery_list_all_suggestions() -> None:
    from slowquery_demo.repositories import slowquery_repository

    mock_s1 = MagicMock()
    mock_s1.fingerprint_id = "fp1"
    mock_s2 = MagicMock()
    mock_s2.fingerprint_id = "fp1"
    mock_s3 = MagicMock()
    mock_s3.fingerprint_id = "fp2"

    session = _mock_session_scalars_all([mock_s1, mock_s2, mock_s3])

    result = await slowquery_repository.list_all_suggestions(session)
    assert "fp1" in result
    assert "fp2" in result
    assert len(result["fp1"]) == 2
    assert len(result["fp2"]) == 1
