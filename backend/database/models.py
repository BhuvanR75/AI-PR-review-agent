"""ORM mapping for the truth lane (L5's relational shape).

Scope note, because it is a deliberate boundary rather than an oversight:
`code_chunks` and `agent_events` are NOT mapped here. They are written and read
through raw asyncpg in `memory/code_memory.py`, `observability/events.py` and the
economics repository, for three reasons:

  1. `code_chunks.embedding` is a pgvector type whose OID the driver has no codec
     for; raw SQL lets us cast a float4[] parameter explicitly and never select
     the vector column itself.
  2. `agent_events` is an append-only hot path, promoted to a hypertable when
     TimescaleDB is present. An ORM adds nothing to an INSERT with no object graph.
  3. Keeping them out of SQLAlchemy keeps `Base.metadata` honest: everything that
     IS mapped corresponds 1:1 with a table the review/HITL code reads.

The DDL in scripts/migrations/ is the source of truth; these classes must match it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PRReviewRecord(Base):
    """One row per review. The identity that ties memory, truth and time together."""

    __tablename__ = "pr_review_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    head_sha: Mapped[str] = mapped_column(Text, nullable=False)
    base_sha: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    delivery_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="queued")
    decision: Mapped[str | None] = mapped_column(Text)
    hitl_mode: Mapped[str | None] = mapped_column(Text)
    overall_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    github_review_id: Mapped[str | None] = mapped_column(Text)
    github_review_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    findings: Mapped[list[FindingRecord]] = relationship(
        back_populates="review", cascade="all, delete-orphan", lazy="selectin"
    )
    hitl: Mapped[HitlReview | None] = relationship(
        back_populates="review", cascade="all, delete-orphan", uselist=False, lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PRReviewRecord {self.repo}#{self.pr_number}@{self.head_sha[:8]} {self.status}>"


class FindingRecord(Base):
    __tablename__ = "finding_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pr_review_records.id", ondelete="CASCADE"), nullable=False
    )
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    agreed_by: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    incomplete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    review: Mapped[PRReviewRecord] = relationship(back_populates="findings")


class DedupNearMiss(Base):
    """A finding dropped by dedup that was *almost* a match. Recorded so the dedup
    rule can be tuned against real data instead of intuition."""

    __tablename__ = "dedup_near_misses"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pr_review_records.id", ondelete="CASCADE"), nullable=False
    )
    kept_dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    dropped_dedup_key: Mapped[str] = mapped_column(Text, nullable=False)
    dropped_agent: Mapped[str] = mapped_column(Text, nullable=False)
    similarity_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class HitlReview(Base):
    __tablename__ = "hitl_reviews"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("pr_review_records.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    state: Mapped[str] = mapped_column(Text, nullable=False, default="awaiting")
    reason: Mapped[str | None] = mapped_column(Text)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    review: Mapped[PRReviewRecord] = relationship(back_populates="hitl")


class HitlFeedback(Base):
    """The learning loop's raw material -- and the thing that is deliberately NOT
    wired into behaviour yet. Acting on feedback before there is enough of it is the
    feedback-loop poisoning failure mode."""

    __tablename__ = "hitl_feedback"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("pr_review_records.id", ondelete="CASCADE"), nullable=False
    )
    finding_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("finding_records.id", ondelete="SET NULL")
    )
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RepoFileIndex(Base):
    """Freshness for the memory lane: only re-embed files whose hash changed.
    Written in bulk by the ingestion pipeline over raw asyncpg; mapped here for
    reporting and staleness queries."""

    __tablename__ = "repo_file_index"

    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    last_indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SystemCapability(Base):
    """Written by scripts/migrate.py so /readyz can report which lanes are live
    without probing extensions on every request."""

    __tablename__ = "system_capabilities"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
