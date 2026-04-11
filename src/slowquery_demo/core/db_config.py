"""Typed accessor for ``DATABASE_URL``.

Lives in ``slowquery_demo`` (not ``alembic/``) so it can be imported by
both the alembic env and unit tests without needing an alembic runtime.
"""

from __future__ import annotations

import os


def get_database_url() -> str:
    """Return the current ``DATABASE_URL`` or raise.

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
    return url
