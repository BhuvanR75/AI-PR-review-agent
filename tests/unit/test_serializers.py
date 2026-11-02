"""Serializer tests, built from in-memory ORM objects so they need no database.

These matter because the console and the CLI read the same shape: a field that goes
missing here is a field that silently disappears from the approval screen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from backend.api.schemas import detail_from, summary_from_row
from backend.database.models import PRReviewRecord
from backend.database.repository import ReviewRow, StoredFinding
from backend.models.enums import (
    AgentType,
    Category,
    Decision,
    HitlState,
    ReviewStatus,
    Severity,
)
from backend.models.findings import Finding
from backend.models.review import ReviewCost


def record(**overrides: object) -> PRReviewRecord:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "repo": "acme/shop",
        "pr_number": 42,
        "head_sha": "9f2c1b7e",
        "title": "Add refunds",
        "author": "dev",
        "status": ReviewStatus.PENDING_APPROVAL.value,
        "decision": Decision.ESCALATE.value,
        "overall_confidence": Decimal("0.421"),
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return PRReviewRecord(**base)  # type: ignore[arg-type]


def finding(severity: Severity = Severity.CRITICAL) -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=severity,
        category=Category.INJECTION,
        summary="interpolated SQL",
        file_path="billing/stripe.py",
        line_start=12,
        confidence=0.86,
        rationale="f-string reaches execute()",
    )


def test_summary_reports_the_queue_fields_the_console_needs() -> None:
    row = ReviewRow(
        review=record(),
        finding_count=3,
        severity_counts={"CRITICAL": 1, "LOW": 2},
        hitl_state=HitlState.AWAITING.value,
    )
    summary = summary_from_row(row, ReviewCost(usd=0.0042, llm_calls=4))

    assert summary.id is not None
    assert summary.repo == "acme/shop"
    assert summary.status is ReviewStatus.PENDING_APPROVAL
    assert summary.decision is Decision.ESCALATE
    assert summary.hitl_state is HitlState.AWAITING
    assert summary.overall_confidence == 0.421
    assert summary.finding_count == 3
    assert summary.severity_counts["CRITICAL"] == 1
    assert summary.cost.usd == 0.0042


def test_summary_survives_a_review_with_no_confidence_or_decision() -> None:
    row = ReviewRow(review=record(status=ReviewStatus.QUEUED.value, decision=None,
                                 overall_confidence=None))
    summary = summary_from_row(row)
    assert summary.overall_confidence is None
    assert summary.decision is None
    assert summary.hitl_state is None
    assert summary.cost.usd == 0.0


def test_detail_includes_findings_and_counts_them_by_severity() -> None:
    stored = [
        StoredFinding(id="f1", finding=finding(Severity.CRITICAL)),
        StoredFinding(id="f2", finding=finding(Severity.HIGH)),
    ]
    detail = detail_from(
        record(),
        stored,
        hitl_state=HitlState.AWAITING,
        cost=ReviewCost(usd=0.01),
    )
    assert detail.finding_count == 2
    assert detail.severity_counts == {"CRITICAL": 1, "HIGH": 1}
    assert [f.severity for f in detail.findings] == [Severity.CRITICAL, Severity.HIGH]
    assert detail.findings[0].locator == "billing/stripe.py:12"


def test_detail_exposes_the_error_list_from_the_spine() -> None:
    """A partial review must say so on the detail page, not only in the published body."""
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    from backend.models.enums import EventType
    from backend.models.review import AgentEvent

    events = [
        AgentEvent(
            ts=_datetime.now(_UTC),
            review_id="r",
            agent=AgentType.TESTS,
            span_id="s",
            event_type=EventType.SPAN_END,
            outcome="incomplete",
        )
    ]
    detail = detail_from(record(), [], hitl_state=None, events=events)
    assert any("incomplete" in error for error in detail.errors)


def test_detail_reports_the_posted_url_when_there_is_one() -> None:
    detail = detail_from(
        record(status=ReviewStatus.POSTED.value, github_review_url="https://example/1"),
        [StoredFinding(id="f1", finding=finding())],
        hitl_state=HitlState.APPROVED,
    )
    assert detail.github_review_url == "https://example/1"
    assert detail.status is ReviewStatus.POSTED
