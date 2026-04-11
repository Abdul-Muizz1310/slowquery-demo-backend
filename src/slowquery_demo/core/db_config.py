"""Typed accessor for ``DATABASE_URL`` + libpq-to-asyncpg URL normalisation.

Lives in ``slowquery_demo`` (not ``alembic/``) so it can be imported by
both the alembic env and unit tests without needing an alembic runtime.

Neon's default connection strings are libpq-flavoured:

    postgresql://user:pass@host/db?sslmode=require&channel_binding=require

asyncpg doesn't understand ``sslmode`` or ``channel_binding`` as query-string
parameters — SQLAlchemy's asyncpg dialect passes them through as kwargs to
``asyncpg.connect`` and asyncpg raises
``TypeError: connect() got an unexpected keyword argument 'sslmode'``.

:func:`normalise_asyncpg_url` rewrites the URL so every caller (alembic,
Settings, tests) gets a clean ``postgresql+asyncpg://.../db?ssl=require``
form. The ``channel_binding`` parameter is dropped entirely — Neon's
server accepts SCRAM-SHA-256 without client-enforced channel binding, and
asyncpg doesn't support it anyway.
"""

from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# libpq query-string keys that asyncpg does not accept and that must be
# stripped from any URL before handing it to SQLAlchemy's asyncpg dialect.
_STRIP_KEYS: frozenset[str] = frozenset({"sslmode", "channel_binding"})


def normalise_asyncpg_url(url: str) -> str:
    """Return a URL that SQLAlchemy's asyncpg dialect can actually open.

    - Adds the ``+asyncpg`` dialect suffix if missing.
    - Drops libpq-only params (``sslmode``, ``channel_binding``).
    - If the original URL asked for TLS via ``sslmode=...``, adds
      ``ssl=<value>`` (the asyncpg spelling) so the connection still
      negotiates TLS.
    """
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    kept: list[tuple[str, str]] = []
    had_sslmode: str | None = None
    for key, value in query_pairs:
        if key == "sslmode":
            had_sslmode = value
            continue
        if key in _STRIP_KEYS:
            continue
        kept.append((key, value))

    # If libpq asked for TLS, ask asyncpg for TLS too.
    if had_sslmode and not any(k == "ssl" for k, _ in kept):
        kept.append(("ssl", had_sslmode))

    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def to_raw_asyncpg_dsn(url: str) -> str:
    """Return a DSN string acceptable to :func:`asyncpg.connect`.

    :func:`normalise_asyncpg_url` produces URLs with the SQLAlchemy
    ``+asyncpg`` dialect suffix, which SQLAlchemy strips internally
    before calling the asyncpg driver. When seed scripts call
    :func:`asyncpg.connect` directly, the suffix has to be removed.

    Also drops ``channel_binding`` (asyncpg doesn't understand it)
    but keeps ``sslmode`` — asyncpg accepts that as a libpq-style
    DSN parameter when embedded in a URL.
    """
    if url.startswith("postgresql+asyncpg://"):
        url = "postgresql://" + url[len("postgresql+asyncpg://") :]

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in query_pairs if k != "channel_binding"]
    new_query = urlencode(kept)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def get_database_url() -> str:
    """Return the current ``DATABASE_URL`` (normalised) or raise.

    Raises:
        RuntimeError: if ``DATABASE_URL`` is unset or empty. The message
            mentions the variable name so operators know exactly what to set.
    """
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL environment variable is required to run alembic migrations "
            "or connect to the demo database"
        )
    return normalise_asyncpg_url(url)
