"""SQLAlchemy declarative base for slowquery_demo.

All ORM classes inherit from :class:`Base`. Importing any sibling module
under ``slowquery_demo.models`` registers its mapper with ``Base.metadata``,
which ``alembic/env.py`` uses as its ``target_metadata``.
"""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base shared by every table in this service."""
