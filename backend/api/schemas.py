"""Serializers from database rows and the spine to the API contract.

Kept apart from the routers so the console's data shape has one definition. The diff
artifact is persisted at review time (004_github_flow.sql) and served from there, so
the console shows the code exactly as it was reviewed.
"""

from __future__ import annotations

from backend.database.artifacts import ReviewArtifact
from backend.database.models import PRReviewRecord
from backend.database.repository import ReviewRow, StoredFinding
from backend.models.enums import Decision, HitlState, ReviewStatus
from backend.models.review import (
    AgentEvent,
    ReviewCost,
    ReviewDetail,
    ReviewSummary,
)
from backend.observability.audit import run_summary


def summary_from_row(
    row: ReviewRow,
    cost: ReviewCost | None = None,
) -> ReviewSummary:
    review: PRReviewRecord = row.review
    return ReviewSummary(
        id=str(review.id),
        repo=review.repo,
        pr_number=review.pr_number,
        title=review.title,
        author=review.author,
        head_sha=review.head_sha,
        status=ReviewStatus(review.status),
        decision=Decision(review.decision) if review.decision else None,
        hitl_state=HitlState(row.hitl_state) if row.hitl_state else None,
        overall_confidence=float(review.overall_confidence)
        if review.overall_confidence is not None
        else None,
        finding_count=row.finding_count,
        severity_counts=row.severity_counts,
        cost=cost or ReviewCost(),
        created_at=review.created_at,
        updated_at=review.updated_at,
    )


def detail_from(
    review: PRReviewRecord,
    stored: list[StoredFinding],
    *,
    hitl_state: HitlState | None,
    cost: ReviewCost | None = None,
    events: list[AgentEvent] | None = None,
    artifact: ReviewArtifact | None = None,
) -> ReviewDetail:
    errors, _incomplete = run_summary(events or [])
    severity_counts: dict[str, int] = {}
    for item in stored:
        key = item.finding.severity.value
        severity_counts[key] = severity_counts.get(key, 0) + 1

    return ReviewDetail(
        id=str(review.id),
        repo=review.repo,
        pr_number=review.pr_number,
        title=review.title,
        author=review.author,
        head_sha=review.head_sha,
        status=ReviewStatus(review.status),
        decision=Decision(review.decision) if review.decision else None,
        hitl_state=hitl_state,
        overall_confidence=float(review.overall_confidence)
        if review.overall_confidence is not None
        else None,
        finding_count=len(stored),
        severity_counts=severity_counts,
        cost=cost or ReviewCost(),
        created_at=review.created_at,
        updated_at=review.updated_at,
        findings=[item.finding for item in stored],
        finding_ids=[item.id for item in stored],
        errors=errors,
        github_review_url=review.github_review_url,
        diff=artifact.diff if artifact else None,
    )
