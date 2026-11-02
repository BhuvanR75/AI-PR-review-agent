"""What gets posted is a product decision, so the renderer is tested for content, not
just for shape: findings without a line must still be said somewhere, CRITICAL findings
must survive trimming, and a review with no line anchors must not silently become empty.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.hitl.publishing import comment_body, render_review
from backend.models.enums import AgentType, Category, Severity
from backend.models.findings import Finding
from backend.tools.capability_scope import ScopeViolation, scope_for


def finding(
    *,
    severity: Severity = Severity.HIGH,
    line: int | None = 12,
    agent: AgentType = AgentType.SECURITY,
    category: Category = Category.INJECTION,
    confidence: float = 0.8,
) -> Finding:
    return Finding(
        agent_type=agent,
        severity=severity,
        category=category,
        summary="interpolated SQL reaches execute()",
        file_path="billing/stripe.py",
        line_start=line,
        suggestion="use a bound parameter",
        confidence=confidence,
        rationale="the f-string is assembled from caller input",
    )


def test_inline_comment_carries_severity_evidence_and_confidence() -> None:
    body = comment_body(finding())
    assert "HIGH" in body
    assert "the f-string is assembled from caller input" in body
    assert "use a bound parameter" in body
    assert "0.80" in body
    assert "security" in body


def test_inline_comment_notes_independent_agreement() -> None:
    body = comment_body(finding().model_copy(update={"agreed_by": [AgentType.QUALITY]}))
    assert "Independently raised" in body
    assert "quality" in body


def test_findings_with_a_line_become_inline_comments() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding(), finding(line=40)],
        overall_confidence=0.8,
        actor="dev",
    )
    assert len(rendered.comments) == 2
    assert rendered.bodyless == []


def test_findings_without_a_line_are_listed_in_the_body_not_dropped() -> None:
    """A coverage gap still matters even though GitHub cannot anchor it."""
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding(line=None, category=Category.MISSING_TEST, agent=AgentType.TESTS)],
        overall_confidence=0.6,
        actor="dev",
    )
    assert rendered.comments == []
    assert len(rendered.bodyless) == 1
    assert "without a specific line" in rendered.body
    assert "missing-test" in rendered.body


def test_there_is_no_path_that_produces_an_empty_review() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding(line=None), finding(line=None, category=Category.OTHER)],
        overall_confidence=0.5,
        actor="dev",
    )
    assert rendered.body.strip()
    # Both are listed: merging duplicates is the aggregator's job, and by the time a
    # review reaches publication the findings it holds are already merged.
    assert len(rendered.bodyless) == 2


def test_critical_findings_are_never_trimmed_away() -> None:
    findings = [finding(line=i, severity=Severity.LOW) for i in range(1, 20)]
    findings.append(finding(line=99, severity=Severity.CRITICAL))
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=findings,
        overall_confidence=0.9,
        actor="dev",
        max_comments=5,
    )
    assert any(f.severity is Severity.CRITICAL for f, _ in rendered.comments)
    assert len(rendered.comments) == 5
    # Trimming is not deletion: the rest are summarised in the body.
    assert len(rendered.bodyless) == len(findings) - 5


def test_header_reports_the_severity_breakdown() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=7,
        findings=[finding(severity=Severity.CRITICAL), finding(severity=Severity.LOW, line=30)],
        overall_confidence=0.7,
        actor="dev",
    )
    assert "2 finding(s)" in rendered.body
    assert "CRITICAL" in rendered.body
    assert "#7" in rendered.body


def test_body_names_the_approver_and_the_confidence() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding()],
        overall_confidence=0.66,
        actor="ayush",
    )
    assert "ayush" in rendered.body
    assert "0.66" in rendered.body


def test_incomplete_passes_are_disclosed_as_partial_coverage() -> None:
    """Silence about a timed-out pass would read as a clean review."""
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding()],
        overall_confidence=0.5,
        actor="dev",
        incomplete_passes=1,
    )
    assert "coverage of this diff is partial" in rendered.body
    assert "unverified" in rendered.body


def test_pass_warnings_are_included() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding()],
        overall_confidence=0.5,
        actor="dev",
        agent_errors=["security: circuit open", "docs pass ended incomplete"],
    )
    assert "Pass warnings" in rendered.body
    assert "circuit open" in rendered.body


def test_cost_is_attributed_in_the_body_when_known() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[finding()],
        overall_confidence=0.5,
        actor="dev",
        cost_usd=0.0123,
    )
    assert "0.0123" in rendered.body


def test_findings_are_ordered_worst_first() -> None:
    rendered = render_review(
        repo="acme/shop",
        pr_number=1,
        findings=[
            finding(severity=Severity.INFO, line=1),
            finding(severity=Severity.CRITICAL, line=2),
            finding(severity=Severity.MEDIUM, line=3),
        ],
        overall_confidence=0.9,
        actor="dev",
    )
    order = [f.severity for f, _ in rendered.comments]
    assert order == [Severity.CRITICAL, Severity.MEDIUM, Severity.INFO]


# --- the enforcement point ---------------------------------------------------
def test_a_specialist_scope_cannot_publish() -> None:
    """Checked before any database access, so the refusal is unconditional."""
    from backend.hitl.publishing import publish_review
    from backend.integrations.github_client import DryRunGitHubClient
    from backend.settings import get_settings

    with pytest.raises(ScopeViolation):
        asyncio.run(
            publish_review(
                review_id="88888888-8888-8888-8888-888888888888",
                repo="acme/shop",
                pr_number=1,
                head_sha="abc",
                findings=[finding()],
                actor="security",
                github=DryRunGitHubClient(get_settings()),
                scope=scope_for("security"),
            )
        )
