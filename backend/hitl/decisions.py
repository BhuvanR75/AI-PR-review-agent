"""Human decisions: approve, reject, re-run.

Three rules are enforced here rather than trusted to the caller.

  * A review can only be decided while it is pending approval, and never after it has
    been superseded by a newer commit. Approving stale code is the outcome the whole
    design exists to prevent.
  * Rejecting records a feedback row per finding, not just one per review, because
    per-agent rejection rates are the drift signal and a review-level row cannot
    produce them.
  * A re-run clears the previous findings first, otherwise the second pass's findings
    land beside the first pass's and the review silently doubles.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from backend.core.exceptions import InvalidReviewState, ReviewNotFound, ReviewSuperseded
from backend.core.workflow_engine import WorkflowEngine
from backend.database.pg import get_pool
from backend.database.repository import HitlRepository, ReviewRepository, StoredFinding
from backend.database.session import session_scope
from backend.economics.cost_repository import CostRepository
from backend.hitl.publishing import PublishResult, publish_review
from backend.integrations.github_client import GitHubClient
from backend.models.enums import (
    AgentType,
    EventType,
    FeedbackKind,
    HitlState,
    Outcome,
    ReviewStatus,
)
from backend.models.review import ReviewRequest
from backend.observability.audit import run_summary
from backend.observability.events import read_trace
from backend.settings import Settings, get_settings
from backend.tools.capability_scope import OPERATOR_SCOPE

logger = logging.getLogger(__name__)

DECIDABLE_STATUSES = frozenset({ReviewStatus.PENDING_APPROVAL.value})


@dataclass
class DecisionOutcome:
    review_id: str
    status: ReviewStatus
    hitl_state: HitlState
    message: str
    posted_finding_ids: list[str] = field(default_factory=list)
    github_review_url: str | None = None
    dry_run: bool = False
    comment_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


class HitlService:
    """The approval console's backend, shared by the HTTP API and the CLI."""

    def __init__(
        self,
        *,
        github: GitHubClient,
        engine: WorkflowEngine | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.github = github
        self.engine = engine
        self.settings = settings or get_settings()
        self.costs = CostRepository(get_pool(), self.settings)

    # --- reads ---------------------------------------------------------------
    async def get_review_and_findings(
        self, review_id: str
    ) -> tuple[Any, list[StoredFinding], HitlState | None]:
        async with session_scope() as session:
            reviews = ReviewRepository(session)
            hitl = HitlRepository(session)
            review = await reviews.get(review_id)
            if review is None:
                raise ReviewNotFound(review_id)
            stored = await reviews.list_findings(review_id)
            record = await hitl.get_for_review(review_id)
            return review, stored, HitlState(record.state) if record else None

    # --- decisions -----------------------------------------------------------
    async def approve(
        self,
        review_id: str,
        *,
        actor: str,
        note: str | None = None,
        finding_ids: list[str] | None = None,
    ) -> DecisionOutcome:
        review, stored, hitl_state = await self.get_review_and_findings(review_id)
        self._assert_decidable(review, hitl_state)

        selected = self._select(stored, finding_ids)
        if not selected:
            raise InvalidReviewState(
                "no findings selected: approving with an empty selection would post a review "
                "with nothing in it"
            )

        findings = [s.finding for s in selected]
        errors, incomplete, cost = await self._run_summary(review_id)

        result: PublishResult = await publish_review(
            review_id=review_id,
            repo=review.repo,
            pr_number=review.pr_number,
            head_sha=review.head_sha,
            findings=findings,
            actor=actor,
            github=self.github,
            overall_confidence=float(review.overall_confidence or 0.0),
            agent_errors=errors,
            incomplete_passes=incomplete,
            cost_usd=cost,
            scope=OPERATOR_SCOPE,
            settings=self.settings,
        )

        async with session_scope() as session:
            hitl = HitlRepository(session)
            await hitl.decide(review_id, HitlState.APPROVED, actor=actor, notes=note)
            if finding_ids is not None:
                # Recorded because "approved only these" is real signal: the rest were
                # rejected in effect, and that is what calibration will need.
                for dropped in stored:
                    if dropped.id not in {s.id for s in selected}:
                        await hitl.add_feedback(
                            review_id,
                            actor=actor,
                            kind=FeedbackKind.EDITED.value,
                            comment="excluded at approval time",
                            finding_id=dropped.id,
                        )

        await self._log_decision(review_id, actor, Outcome.APPROVED, "human approved and posted")

        return DecisionOutcome(
            review_id=review_id,
            status=ReviewStatus.POSTED,
            hitl_state=HitlState.APPROVED,
            message=f"posted {result.comment_count} comment(s) via {self.github.name}",
            posted_finding_ids=[s.id for s in selected],
            github_review_url=result.github_review_url,
            dry_run=result.dry_run,
            comment_count=result.comment_count,
        )

    async def reject(
        self,
        review_id: str,
        *,
        actor: str,
        reason: str,
    ) -> DecisionOutcome:
        review, stored, hitl_state = await self.get_review_and_findings(review_id)
        self._assert_decidable(review, hitl_state)

        # GitHub first, database second: if the token cannot comment or close, the
        # call raises before any local state changes, so the review stays pending and
        # the rejection can be retried once the permission is fixed. Recording
        # "rejected" locally while the PR stays open would be a lie on both sides.
        closed = await self.github.close_pull_request(
            review.repo,
            review.pr_number,
            reason=reason,
            actor=actor,
            review_id=review_id,
        )

        async with session_scope() as session:
            reviews = ReviewRepository(session)
            hitl = HitlRepository(session)
            await hitl.decide(review_id, HitlState.REJECTED, actor=actor, notes=reason)
            await reviews.set_status(review_id, ReviewStatus.REJECTED)
            await hitl.add_feedback(
                review_id,
                actor=actor,
                kind=FeedbackKind.REJECTED.value,
                comment=reason,
            )
            for finding in stored:
                await hitl.add_feedback(
                    review_id,
                    actor=actor,
                    kind=FeedbackKind.REJECTED.value,
                    comment=reason,
                    finding_id=finding.id,
                )

        await self._log_decision(
            review_id,
            actor,
            Outcome.REJECTED,
            f"{reason} (commented={closed.commented}, closed={closed.closed})",
        )

        where = "dry run: nothing was sent to GitHub" if closed.dry_run else closed.summary
        return DecisionOutcome(
            review_id=review_id,
            status=ReviewStatus.REJECTED,
            hitl_state=HitlState.REJECTED,
            message=f"rejected: {where}; recorded feedback against {len(stored)} finding(s)",
            github_review_url=closed.comment_url,
            dry_run=closed.dry_run,
            comment_count=1 if closed.commented else 0,
            extra={"reason": reason, "pr_closed": closed.closed},
        )

    async def rerun(
        self,
        review_id: str,
        *,
        actor: str,
        note: str | None = None,
        agents: list[AgentType] | None = None,
    ) -> DecisionOutcome:
        if self.engine is None:
            raise InvalidReviewState("re-run requires a configured workflow engine")

        review, stored, hitl_state = await self.get_review_and_findings(review_id)
        if review.status == ReviewStatus.SUPERSEDED.value:
            raise ReviewSuperseded(review_id)
        if review.status == ReviewStatus.POSTED.value:
            raise InvalidReviewState(
                "this review has already been posted; a re-run would post a second review"
            )

        async with session_scope() as session:
            reviews = ReviewRepository(session)
            hitl = HitlRepository(session)
            cleared = await reviews.delete_findings(review_id)
            await reviews.set_status(review_id, ReviewStatus.QUEUED, error=None)
            await hitl.enqueue(review_id, reason="awaiting re-run")
            await hitl.add_feedback(
                review_id,
                actor=actor,
                kind=FeedbackKind.EDITED.value,
                comment=note or "re-run requested",
                payload={"agents": [a.value for a in agents] if agents else None},
            )

        await self._log_decision(
            review_id,
            actor,
            Outcome.OK,
            f"re-run requested (cleared {cleared} previous finding(s))",
        )

        # A fresh thread id, so the re-run does not resume the previous attempt's
        # checkpoint and append its findings to this one's.
        request = ReviewRequest(
            review_id=review_id,
            repo=review.repo,
            pr_number=review.pr_number,
            head_sha=review.head_sha,
            delivery_id=review.delivery_id,
        )
        thread_id = f"{review_id}:{uuid.uuid4().hex[:8]}"
        outcome = await self.engine.run(request, thread_id=thread_id)

        return DecisionOutcome(
            review_id=review_id,
            status=outcome.status,
            hitl_state=HitlState.AWAITING
            if outcome.status is ReviewStatus.PENDING_APPROVAL
            else hitl_state or HitlState.AWAITING,
            message=(
                f"re-ran {len(agents)} pass(es)"
                if agents
                else "re-ran all passes"
            )
            + f"; {outcome.finding_count} finding(s), status {outcome.status.value}",
            extra={
                "cleared_findings": cleared,
                "overall_confidence": outcome.overall_confidence,
                "errors": outcome.errors,
            },
        )

    # --- helpers -------------------------------------------------------------
    def _assert_decidable(self, review: Any, hitl_state: HitlState | None) -> None:
        if review.status == ReviewStatus.SUPERSEDED.value:
            raise ReviewSuperseded(str(review.id))
        if review.status not in DECIDABLE_STATUSES:
            raise InvalidReviewState(
                f"review is '{review.status}', expected one of {sorted(DECIDABLE_STATUSES)}"
            )
        if hitl_state is not None and hitl_state is not HitlState.AWAITING:
            raise InvalidReviewState(f"this review was already {hitl_state.value}")

    @staticmethod
    def _select(stored: list[StoredFinding], finding_ids: list[str] | None) -> list[StoredFinding]:
        if finding_ids is None:
            return list(stored)
        wanted = set(finding_ids)
        return [s for s in stored if s.id in wanted]

    async def _run_summary(self, review_id: str) -> tuple[list[str], int, float]:
        """Errors, incomplete passes and cost, read from the spine rather than stored
        alongside the review -- the same single source the trace viewer uses."""
        try:
            events = await read_trace(review_id)
        except Exception:  # noqa: BLE001 - a missing spine must not block a decision
            events = []

        errors, incomplete = run_summary(events)

        try:
            cost = (await self.costs.review_cost(review_id)).usd
        except Exception:  # noqa: BLE001
            cost = 0.0
        return errors, incomplete, cost

    async def _log_decision(
        self, review_id: str, actor: str, outcome: Outcome, reason: str
    ) -> None:
        from backend.observability.events import emit_agent_event

        await emit_agent_event(
            agent=str(AgentType.AGGREGATOR),
            event_type=EventType.DECISION,
            review_id=review_id,
            outcome=outcome.value,
            payload={"actor": actor, "reason": reason, "surface": "hitl"},
        )
