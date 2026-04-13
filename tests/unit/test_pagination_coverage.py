"""Unit tests for pagination coverage — clamp_limit edge cases."""

from __future__ import annotations

from slowquery_demo.schemas.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    clamp_limit,
    decode_cursor,
    encode_cursor,
)


def test_clamp_limit_none_returns_default() -> None:
    assert clamp_limit(None) == DEFAULT_PAGE_SIZE


def test_clamp_limit_zero_returns_default() -> None:
    assert clamp_limit(0) == DEFAULT_PAGE_SIZE


def test_clamp_limit_negative_returns_default() -> None:
    assert clamp_limit(-5) == DEFAULT_PAGE_SIZE


def test_clamp_limit_within_range() -> None:
    assert clamp_limit(50) == 50


def test_clamp_limit_above_max_returns_max() -> None:
    assert clamp_limit(1000) == MAX_PAGE_SIZE


def test_encode_decode_cursor_roundtrip() -> None:
    encoded = encode_cursor("2025-01-01T00:00:00Z", "abc-123")
    decoded = decode_cursor(encoded)
    assert decoded.created_at == "2025-01-01T00:00:00Z"
    assert decoded.id == "abc-123"
