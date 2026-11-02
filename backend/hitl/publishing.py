"""Publication: the one path a review can take to GitHub.

Both the human-approval path and the (currently disabled) auto-post path come through
here, so there is exactly one place that decides what gets posted and exactly one
place that enforces the POST_REVIEW capability. The scope check is not decorative: a
specialist cannot post, and this function refuses if the caller's scope does not grant
it, regardless of which code path reached it.

The supersede check happens here rather than at approval time, because the gap between
"a human approved this" and "it was posted" is where a new commit could have landed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.core.exceptions import ReviewNotFound, ReviewSuperseded
from backend.database.repository import HitlRepository, ReviewRepository
from backend.database.session import session_scope
from backend.integrations.github_client import GitHubClient
from backend.models.enums import (
    SEVERITY_RANK,
    AgentType,
    EventType,
    FeedbackKind,
    Outcome,
    ReviewStatus,
    Severity,
)
from backend.models.findings import Finding
from backend.observability.events import emit_agent_event
from backend.settings import Settings, get_settings
from backend.tools.capability_scope import OPERATOR_SCOPE, Capability, CapabilityScope

logger = logging.getLogger(__name__)

REVIEW_HEADER = "## Automated review"

PROVENANCE_FOOTER = (
    "_Findings come from four specialist passes over this diff, each grounded in "
    "retrieved repository context. Every finding carries the evidence it was raised "
    "on, and confidence is the pass's own estimate rather than a calibrated "
    "probability -- read it as a hint about how much scrutiny a finding deserves._"
)


@dataclass
class PublishResult:
    review_id: str
    posted: bool
    github_review_id: str | None = None
    github_review_url: str | None = None
    comment_count: int = 0
    bodyless_findings: int = 0
    dry_run: bool = False
    summary: str = ""


@dataclass
class RenderedReview:
    body: str
    comments: list[tuple[Finding, str]] = field(default_factory=list)
    bodyless: list[Finding] = field(default_factory=list)


def _severity_badge(severity: Severity) -> str:
    return {
        Severity.CRITICAL: "**CRITICAL**",
        Severity.HIGH: "**HIGH**",
        Severity.MEDIUM: "MEDIUM",
        Severity.LOW: "LOW",
        Severity.INFO: "INFO",
    }[severity]


def comment_body(finding: Finding) -> str:
    """The inline comment for one finding."""
    lines = [
        f"{_severity_badge(finding.severity)} `{finding.category}` — {finding.summary}",
        "",
        f"**Why:** {finding.rationale}",
    ]
    if finding.suggestion:
        lines += ["", f"**Suggestion:** {finding.suggestion}"]
    if finding.agreed_by:
        agree = ", ".join(a.value for a in finding.agreed_by)
        lines += ["", f"_Independently raised by the {agree} pass(es)._"]
    lines += [
        "",
        f"_Raised by the {finding.agent_type.value} pass · confidence {finding.confidence:.2f}_",
    ]
    return "\n".join(lines)


def render_review(
    *,
    repo: str,
    pr_number: int,
    findings: list[Finding],
    overall_confidence: float,
    actor: str,
    agent_errors: list[str] | None = None,
    incomplete_passes: int = 0,
    cost_usd: float = 0.0,
    max_comments: int = 20,
) -> RenderedReview:
    """Build the review body and the inline comments.

    Findings with a line anchor become inline comments; findings without one are
    listed in the body. They are not dropped -- a coverage gap with no specific line
    is still worth saying -- but GitHub cannot anchor them, and pretending otherwise
    produces a silently rejected comment.
    """
    inline: list[tuple[Finding, str]] = []
    bodyless: list[Finding] = []

    ordered = sorted(
        findings,
        key=lambda f: (-SEVERITY_RANK[f.severity], -f.confidence, f.file_path, f.line_start or 0),
    )
    for finding in ordered:
        if finding.line_start is not None and len(inline) < max_comments:
            inline.append((finding, comment_body(finding)))
        else:
            bodyless.append(finding)

    counts: dict[str, int] = {}
    for finding in ordered:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    count_line = ", ".join(f"{n} {sev}" for sev, n in counts.items()) or "none"

    lines = [
        f"{REVIEW_HEADER} — {len(ordered)} finding(s): {count_line}",
        "",
        f"Approved by `{actor}` before posting. Overall confidence: {overall_confidence:.2f}.",
    ]

    if agent_errors:
        lines += ["", "<details><summary>Pass warnings</summary>", ""]
        lines += [f"- {error}" for error in agent_errors[:10]]
        lines += ["", "</details>"]

    if incomplete_passes:
        lines += [
            "",
            f"> {incomplete_passes} pass(es) did not complete, so coverage of this diff is "
            f"partial. Treat an empty category as unverified rather than clean.",
        ]

    if bodyless:
        lines += ["", "### Findings without a specific line"]
        for finding in bodyless:
            location = f"`{finding.file_path}`"
            lines += [
                "",
                f"- {_severity_badge(finding.severity)} `{finding.category}` {location} — "
                f"{finding.summary}",
                f"  - {finding.rationale}",
            ]

    lines += ["", "---", PROVENANCE_FOOTER]
    if cost_usd:
        lines += ["", f"_Review cost: ${cost_usd:.4f} (attributed from the events spine)._"]
    lines += ["", f"_Repository: `{repo}` · pull request #{pr_number}_"]

    return RenderedReview(body="\n".join(lines), comments=inline, bodyless=bodyless)


async def publish_review(
    *,
    review_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    findings: list[Finding],
    actor: str,
    github: GitHubClient,
    overall_confidence: float = 0.0,
    agent_errors: list[str] | None = None,
    incomplete_passes: int = 0,
    cost_usd: float = 0.0,
    scope: CapabilityScope | None = None,
    settings: Settings | None = None,
) -> PublishResult:
    resolved = settings or get_settings()
    active_scope = scope or OPERATOR_SCOPE

    # The enforcement point: publication requires the capability, whoever is asking.
    active_scope.require(Capability.POST_REVIEW)

    rendered = render_review(
        repo=repo,
        pr_number=pr_number,
        findings=findings,
        overall_confidence=overall_confidence,
        actor=actor,
        agent_errors=agent_errors,
        incomplete_passes=incomplete_passes,
        cost_usd=cost_usd,
        max_comments=resolved.max_comments_per_review,
    )

    async with session_scope() as session:
        reviews = ReviewRepository(session)
        review = await reviews.get(review_id)
        if review is None:
            raise ReviewNotFound(review_id)
        if review.status == ReviewStatus.SUPERSEDED.value:
            raise ReviewSuperseded(review_id)
        if review.head_sha != head_sha and head_sha:
            # A different commit is now the head of this PR: refuse rather than
            # attach stale comments to a commit that no longer exists.
            raise ReviewSuperseded(review_id)

    from backend.integrations.github_models import ReviewComment

    comments = [
        ReviewComment(path=finding.file_path, line=int(finding.line_start or 1), body=body)
        for finding, body in rendered.comments
    ]

    posted = await github.post_review(
        repo, pr_number, head_sha=head_sha, body=rendered.body, comments=comments
    )

    await emit_agent_event(
        agent=str(AgentType.AGGREGATOR),
        event_type=EventType.TOOL_CALL,
        review_id=review_id,
        outcome=Outcome.APPROVED.value,
        payload={
            "tool": "post_review",
            "github_review_id": posted.id,
            "comments": posted.comment_count,
            "bodyless": len(rendered.bodyless),
            "dry_run": posted.dry_run,
            "actor": actor,
        },
    )

    async with session_scope() as session:
        reviews = ReviewRepository(session)
        hitl = HitlRepository(session)
        await reviews.attach_github_review(review_id, posted.id, posted.url)
        await hitl.add_feedback(
            review_id,
            actor=actor,
            kind=FeedbackKind.APPROVED.value,
            comment=f"posted review {posted.id}",
            payload={
                "comments": posted.comment_count,
                "dry_run": posted.dry_run,
                "github_review_id": posted.id,
            },
        )

    logger.info(
        "published review",
        extra={
            "context": {
                "review_id": review_id,
                "comments": posted.comment_count,
                "dry_run": posted.dry_run,
            }
        },
    )
    return PublishResult(
        review_id=review_id,
        posted=True,
        github_review_id=posted.id,
        github_review_url=posted.url,
        comment_count=posted.comment_count,
        bodyless_findings=len(rendered.bodyless),
        dry_run=posted.dry_run,
        summary=posted.summary,
    )


__all__ = [
    "PublishResult",
    "RenderedReview",
    "comment_body",
    "publish_review",
    "render_review",
]
