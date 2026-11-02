"""Repositories over the truth lane. The only relational API the application uses.

Design notes worth keeping:

  * `upsert_review_from_event` is the idempotency spine at the *review* level. The
    ingress dedupes redelivered webhooks by delivery id; this dedupes by
    (repo, pr, head_sha), so a retried job, a re-run, or a second webhook for the
    same commit all land on one review row instead of producing two reviews.
  * Superseding is explicit. A new push marks the previous head's review
    `superseded` and closes its HITL row, so a human can never approve a review of
    code that no longer exists.
  * A finding's rationale and confidence are stored, not recomputed. The audit
    trail has to be able to reproduce what the reviewer saw at the time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.models import (
    DedupNearMiss,
    FindingRecord,
    HitlFeedback,
    HitlReview,
    PRReviewRecord,
)
from backend.models.enums import AgentType, Category, HitlState, ReviewStatus, Severity
from backend.models.findings import Finding
from backend.models.webhook import WebhookEvent

#: Reviews in these states may be safely re-driven; anything else is already
#: resolved and must not be silently overwritten.
REDRIVABLE_STATUSES = frozenset(
    {ReviewStatus.QUEUED.value, ReviewStatus.RUNNING.value, ReviewStatus.FAILED.value}
)


@dataclass(frozen=True)
class StoredFinding:
    """A finding plus the database identity a human decision needs."""

    id: str
    finding: Finding


@dataclass(frozen=True)
class ReviewRow:
    """A review plus the aggregate columns a list view needs, fetched together."""

    review: PRReviewRecord
    finding_count: int = 0
    severity_counts: dict[str, int] = field(default_factory=dict)
    hitl_state: str | None = None


@dataclass(frozen=True)
class UpsertResult:
    review: PRReviewRecord
    created: bool
    redrivable: bool


def _as_decimal(value: float) -> Decimal:
    return Decimal(str(round(float(value), 3)))


def finding_from_record(row: FindingRecord) -> Finding:
    return Finding(
        agent_type=AgentType(row.agent_type),
        severity=Severity(row.severity),
        category=Category(row.category),
        summary=row.summary,
        file_path=row.file_path,
        line_start=row.line_start,
        line_end=row.line_end,
        suggestion=row.suggestion,
        confidence=float(row.confidence),
        rationale=row.rationale,
        agreed_by=[AgentType(a) for a in (row.agreed_by or [])],
        incomplete=bool(row.incomplete),
    )


def stored_finding(row: FindingRecord) -> StoredFinding:
    return StoredFinding(id=str(row.id), finding=finding_from_record(row))


class ReviewRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # --- writes --------------------------------------------------------------
    async def upsert_review_from_event(self, event: WebhookEvent) -> UpsertResult:
        stmt = (
            pg_insert(PRReviewRecord)
            .values(
                repo=event.repo,
                pr_number=event.pr_number,
                head_sha=event.head_sha,
                base_sha=event.base_sha,
                title=event.title,
                author=event.author,
                delivery_id=event.delivery_id,
                status=ReviewStatus.QUEUED.value,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    PRReviewRecord.repo,
                    PRReviewRecord.pr_number,
                    PRReviewRecord.head_sha,
                ]
            )
            .returning(PRReviewRecord.id)
        )
        inserted_id = (await self.session.execute(stmt)).scalar_one_or_none()

        if inserted_id is not None:
            review = await self.get(str(inserted_id))
            assert review is not None  # noqa: S101 - just inserted
            return UpsertResult(review=review, created=True, redrivable=True)

        existing = await self.get_by_head(event.repo, event.pr_number, event.head_sha)
        if existing is None:
            # Lost a race with a concurrent insert; the caller can retry.
            raise RuntimeError("review row vanished between insert and select")
        return UpsertResult(
            review=existing,
            created=False,
            redrivable=existing.status in REDRIVABLE_STATUSES,
        )

    async def create_review(
        self,
        *,
        repo: str,
        pr_number: int,
        head_sha: str,
        base_sha: str | None = None,
        title: str | None = None,
        author: str | None = None,
        delivery_id: str | None = None,
        status: ReviewStatus = ReviewStatus.QUEUED,
    ) -> PRReviewRecord:
        review = PRReviewRecord(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            base_sha=base_sha,
            title=title,
            author=author,
            delivery_id=delivery_id,
            status=status.value,
        )
        self.session.add(review)
        await self.session.flush()
        await self.session.refresh(review)
        return review

    async def supersede_previous_heads(
        self, repo: str, pr_number: int, keep_head_sha: str
    ) -> list[str]:
        """Mark every other head of this PR as superseded. Returns the affected ids.

        Two effects, both deliberate: the stale review can no longer be posted, and
        its HITL row leaves the approval queue.
        """
        stmt = (
            update(PRReviewRecord)
            .where(
                PRReviewRecord.repo == repo,
                PRReviewRecord.pr_number == pr_number,
                PRReviewRecord.head_sha != keep_head_sha,
                PRReviewRecord.status.notin_(
                    [ReviewStatus.POSTED.value, ReviewStatus.SUPERSEDED.value]
                ),
            )
            .values(status=ReviewStatus.SUPERSEDED.value, updated_at=func.now())
            .returning(PRReviewRecord.id)
        )
        ids = [str(row) for row in (await self.session.execute(stmt)).scalars().all()]
        if ids:
            await self.session.execute(
                update(HitlReview)
                .where(
                    HitlReview.review_id.in_([uuid.UUID(i) for i in ids]),
                    HitlReview.state == HitlState.AWAITING.value,
                )
                .values(state=HitlState.SUPERSEDED.value, notes="superseded by a newer commit")
            )
        return ids

    async def set_status(
        self,
        review_id: str,
        status: ReviewStatus,
        *,
        decision: str | None = None,
        overall_confidence: float | None = None,
        error: str | None = None,
        hitl_mode: str | None = None,
        posted: bool = False,
    ) -> None:
        values: dict[str, Any] = {
            "status": status.value,
            "updated_at": func.now(),
            "error": error,
        }
        if decision is not None:
            values["decision"] = decision
        if overall_confidence is not None:
            values["overall_confidence"] = _as_decimal(overall_confidence)
        if hitl_mode is not None:
            values["hitl_mode"] = hitl_mode
        if posted:
            values["posted_at"] = datetime.now(UTC)
        await self.session.execute(
            update(PRReviewRecord).where(PRReviewRecord.id == uuid.UUID(review_id)).values(**values)
        )

    async def attach_github_review(
        self, review_id: str, github_review_id: str, url: str | None
    ) -> None:
        await self.session.execute(
            update(PRReviewRecord)
            .where(PRReviewRecord.id == uuid.UUID(review_id))
            .values(
                github_review_id=github_review_id,
                github_review_url=url,
                status=ReviewStatus.POSTED.value,
                posted_at=datetime.now(UTC),
                updated_at=func.now(),
            )
        )

    # --- reads ---------------------------------------------------------------
    async def get(self, review_id: str) -> PRReviewRecord | None:
        return await self.session.get(PRReviewRecord, uuid.UUID(review_id))

    async def get_by_head(self, repo: str, pr_number: int, head_sha: str) -> PRReviewRecord | None:
        stmt = select(PRReviewRecord).where(
            PRReviewRecord.repo == repo,
            PRReviewRecord.pr_number == pr_number,
            PRReviewRecord.head_sha == head_sha,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_rows(
        self,
        *,
        statuses: list[ReviewStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ReviewRow]:
        stmt = select(PRReviewRecord, HitlReview.state).outerjoin(
            HitlReview, HitlReview.review_id == PRReviewRecord.id
        )
        if statuses:
            stmt = stmt.where(PRReviewRecord.status.in_([s.value for s in statuses]))
        stmt = stmt.order_by(PRReviewRecord.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).all()
        if not rows:
            return []

        ids = [r[0].id for r in rows]
        counts = await self._severity_counts(ids)
        return [
            ReviewRow(
                review=review,
                finding_count=sum(counts.get(review.id, {}).values()),
                severity_counts=counts.get(review.id, {}),
                hitl_state=hitl_state,
            )
            for review, hitl_state in rows
        ]

    async def _severity_counts(
        self, review_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, dict[str, int]]:
        stmt = (
            select(FindingRecord.review_id, FindingRecord.severity, func.count())
            .where(FindingRecord.review_id.in_(review_ids))
            .group_by(FindingRecord.review_id, FindingRecord.severity)
        )
        out: dict[uuid.UUID, dict[str, int]] = {}
        for review_id, severity, count in (await self.session.execute(stmt)).all():
            out.setdefault(review_id, {})[severity] = int(count)
        return out

    async def status_counts(self) -> dict[str, int]:
        stmt = select(PRReviewRecord.status, func.count()).group_by(PRReviewRecord.status)
        return {status: int(count) for status, count in (await self.session.execute(stmt)).all()}

    async def oldest_pending_age_seconds(self) -> float | None:
        """Escalation-rate monitoring starts here: how long has the queue been stuck?"""
        stmt = select(func.min(PRReviewRecord.created_at)).where(
            PRReviewRecord.status == ReviewStatus.PENDING_APPROVAL.value
        )
        oldest = (await self.session.execute(stmt)).scalar_one_or_none()
        if oldest is None:
            return None
        return (datetime.now(UTC) - oldest).total_seconds()

    # --- findings ------------------------------------------------------------
    async def store_findings(self, review_id: str, findings: list[Finding]) -> list[StoredFinding]:
        rid = uuid.UUID(review_id)
        rows = [
            FindingRecord(
                review_id=rid,
                agent_type=f.agent_type.value,
                severity=f.severity.value,
                category=f.category.value,
                summary=f.summary,
                file_path=f.file_path,
                line_start=f.line_start,
                line_end=f.line_end,
                suggestion=f.suggestion,
                confidence=_as_decimal(f.confidence),
                rationale=f.rationale,
                dedup_key=f.dedup_key,
                agreed_by=[a.value for a in f.agreed_by],
                incomplete=f.incomplete,
            )
            for f in findings
        ]
        self.session.add_all(rows)
        await self.session.flush()
        return [stored_finding(r) for r in rows]

    async def list_findings(self, review_id: str) -> list[StoredFinding]:
        stmt = (
            select(FindingRecord)
            .where(FindingRecord.review_id == uuid.UUID(review_id))
            .order_by(FindingRecord.severity, FindingRecord.file_path, FindingRecord.line_start)
        )
        return [stored_finding(r) for r in (await self.session.execute(stmt)).scalars().all()]

    async def get_findings_by_ids(self, finding_ids: list[str]) -> list[StoredFinding]:
        if not finding_ids:
            return []
        stmt = select(FindingRecord).where(
            FindingRecord.id.in_([uuid.UUID(i) for i in finding_ids])
        )
        return [stored_finding(r) for r in (await self.session.execute(stmt)).scalars().all()]

    async def delete_findings(self, review_id: str) -> int:
        """Clear a review's findings before a re-run, so the new pass replaces them
        rather than doubling them."""
        result = await self.session.execute(
            delete(FindingRecord).where(FindingRecord.review_id == uuid.UUID(review_id))
        )
        # CursorResult carries rowcount; the generic Result type does not declare it.
        return int(getattr(result, "rowcount", 0) or 0)

    async def record_near_misses(
        self,
        review_id: str,
        misses: list[dict[str, str]],
    ) -> None:
        rid = uuid.UUID(review_id)
        self.session.add_all(
            [
                DedupNearMiss(
                    review_id=rid,
                    kept_dedup_key=m["kept"],
                    dropped_dedup_key=m["dropped"],
                    dropped_agent=m["agent"],
                    similarity_reason=m.get("reason"),
                )
                for m in misses
            ]
        )
        await self.session.flush()


class HitlRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(self, review_id: str, reason: str) -> HitlReview:
        """Put a review in the human queue. Idempotent per review."""
        rid = uuid.UUID(review_id)
        existing = await self.get_for_review(review_id)
        if existing is not None:
            existing.state = HitlState.AWAITING.value
            existing.reason = reason
            await self.session.flush()
            return existing
        hitl = HitlReview(review_id=rid, state=HitlState.AWAITING.value, reason=reason)
        self.session.add(hitl)
        await self.session.flush()
        return hitl

    async def get_for_review(self, review_id: str) -> HitlReview | None:
        stmt = select(HitlReview).where(HitlReview.review_id == uuid.UUID(review_id))
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def decide(
        self,
        review_id: str,
        state: HitlState,
        *,
        actor: str,
        notes: str | None = None,
    ) -> HitlReview | None:
        hitl = await self.get_for_review(review_id)
        if hitl is None:
            return None
        hitl.state = state.value
        hitl.decided_by = actor
        hitl.decided_at = datetime.now(UTC)
        if notes:
            hitl.notes = notes
        await self.session.flush()
        return hitl

    async def add_feedback(
        self,
        review_id: str,
        *,
        actor: str,
        kind: str,
        comment: str | None = None,
        finding_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HitlFeedback:
        row = HitlFeedback(
            review_id=uuid.UUID(review_id),
            finding_id=uuid.UUID(finding_id) if finding_id else None,
            actor=actor,
            kind=kind,
            comment=comment,
            payload=payload,
        )
        self.session.add(row)
        await self.session.flush()
        return row
