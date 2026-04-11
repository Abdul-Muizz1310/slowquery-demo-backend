"""Typed errors raised by the PostgresStoreWriter."""

from __future__ import annotations


class StoreWriterError(RuntimeError):
    """Raised when a StoreWriter hook fails to persist.

    Wraps the underlying driver exception (asyncpg) so callers can
    distinguish writer failures from unrelated ``RuntimeError``s and
    log them at ``error`` severity with the failing fingerprint id in
    the structlog context.
    """
