"""Remove duplicate suggestions, then guarantee ``uq_suggestions_fp_kind_sql``.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

Why this exists (spec 13): ``0001`` was applied to both live Neon branches on
2026-04-12 and then *edited* to add ``uq_suggestions_fp_kind_sql``. Alembic
never re-runs a recorded revision, so the live branches never got the
constraint, duplicate suggestions piled up, and the store writer's
``ON CONFLICT (fingerprint_id, kind, sql)`` insert failed for want of a
matching constraint. Databases created after the edit already have it.

So this revision must be correct on both kinds of database:

* delete every suggestion that has a twin with a lower ``id`` and the same
  ``(fingerprint_id, kind, sql)`` (``sql`` is nullable; two NULLs count as
  twins), keeping the lowest ``id`` of each group;
* add the constraint only when it is missing.

The table is locked against concurrent writes first, so an instance still
serving the previous release cannot slip a duplicate in between the delete
and the constraint. ``0001`` itself is never edited again.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision: str | None = "0001"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_CONSTRAINT = "uq_suggestions_fp_kind_sql"

_LOCK = "LOCK TABLE suggestions IN SHARE ROW EXCLUSIVE MODE"
_DELETE_DUPLICATES = (
    "DELETE FROM suggestions AS later USING suggestions AS earlier "
    "WHERE later.fingerprint_id = earlier.fingerprint_id "
    "AND later.kind = earlier.kind "
    "AND later.sql IS NOT DISTINCT FROM earlier.sql "
    "AND later.id > earlier.id"
)
_CONSTRAINT_EXISTS = (
    "SELECT 1 FROM pg_constraint WHERE conname = :name AND conrelid = 'suggestions'::regclass"
)


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(_LOCK))
    bind.execute(sa.text(_DELETE_DUPLICATES))
    exists = bind.execute(sa.text(_CONSTRAINT_EXISTS), {"name": _CONSTRAINT}).first()
    if exists is None:
        op.create_unique_constraint(_CONSTRAINT, "suggestions", ["fingerprint_id", "kind", "sql"])


def downgrade() -> None:
    # Deliberately empty. Deleted duplicates cannot be restored, and the
    # constraint belongs to 0001 on every database created from it, so
    # 0001's downgrade (which drops it) works on both kinds of database once
    # this revision has run.
    pass
