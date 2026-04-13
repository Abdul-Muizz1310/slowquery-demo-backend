"""Bookkeeping ORM models written by :class:`PostgresStoreWriter`.

These four tables match the data model in
``slowquery-detective/docs/projects/50-slowquery-detective.md`` verbatim.
They live in their own module so the commerce MVC layer never imports
slowquery internals.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from slowquery_demo.models.base import Base


class QueryFingerprint(Base):
    __tablename__ = "query_fingerprints"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    call_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    total_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    p50_ms: Mapped[float | None] = mapped_column(Numeric)
    p95_ms: Mapped[float | None] = mapped_column(Numeric)
    p99_ms: Mapped[float | None] = mapped_column(Numeric)
    max_ms: Mapped[float | None] = mapped_column(Numeric)


class QuerySample(Base):
    __tablename__ = "query_samples"
    __table_args__ = (
        Index(
            "ix_query_samples_fingerprint_sampled_at",
            "fingerprint_id",
            "sampled_at",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("query_fingerprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    params: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    duration_ms: Mapped[float] = mapped_column(Numeric, nullable=False)
    rows: Mapped[int | None] = mapped_column(Integer)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ExplainPlan(Base):
    __tablename__ = "explain_plans"

    fingerprint_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("query_fingerprints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_json: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    plan_text: Mapped[str] = mapped_column(Text, nullable=False)
    cost: Mapped[float | None] = mapped_column(Numeric)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Suggestion(Base):
    __tablename__ = "suggestions"
    __table_args__ = (
        Index("ix_suggestions_fingerprint_id", "fingerprint_id"),
        UniqueConstraint("fingerprint_id", "kind", "sql", name="uq_suggestions_fp_kind_sql"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    fingerprint_id: Mapped[str] = mapped_column(
        String(16),
        ForeignKey("query_fingerprints.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    sql: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
