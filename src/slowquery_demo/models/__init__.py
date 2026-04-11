"""Domain model registry.

Importing this package runs every ORM-module import below as a side effect
so all mappers register with ``Base.metadata`` before alembic / tests inspect
it. Order is irrelevant — SQLAlchemy resolves cross-table relationships on
first access, not at class-declaration time.
"""

from __future__ import annotations

from slowquery_demo.models import order, order_item, product, slowquery_store, user  # noqa: F401
from slowquery_demo.models.base import Base

__all__ = ["Base"]
