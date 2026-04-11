"""Cursor pagination helpers.

Cursors are opaque base64-encoded JSON payloads ``{"created_at": str,
"id": str}``. The cursor parser raises :class:`InvalidCursorError` on
any malformed input so the endpoint returns 422 via the typed error
handler, never leaking decoder internals.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from slowquery_demo.core.errors import InvalidCursorError

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


class PaginatedResponse[T](BaseModel):
    """Uniform paginated list response."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None


@dataclass(frozen=True)
class Cursor:
    created_at: str
    id: str


def encode_cursor(created_at: str, id: str) -> str:
    payload = json.dumps({"created_at": created_at, "id": id})
    return base64.urlsafe_b64encode(payload.encode()).decode()


def decode_cursor(raw: str) -> Cursor:
    """Decode an opaque cursor or raise :class:`InvalidCursorError`."""
    try:
        payload = base64.urlsafe_b64decode(raw.encode()).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise InvalidCursorError(f"invalid cursor encoding: {exc}") from exc
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InvalidCursorError(f"cursor is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or "created_at" not in data or "id" not in data:
        raise InvalidCursorError("cursor must decode to an object with created_at + id")
    return Cursor(created_at=str(data["created_at"]), id=str(data["id"]))


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if limit <= 0:
        return DEFAULT_PAGE_SIZE
    return min(limit, MAX_PAGE_SIZE)
