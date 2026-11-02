"""The graph's nodes.

Four nodes and one dispatch function. Each does one thing, and the two that touch the
outside world (`prepare` and `gate`) are the only ones allowed to fail a workflow.

That asymmetry is deliberate. `prepare` must fail loudly: if the diff cannot be
fetched there is nothing to review, and pretending otherwise would produce a confident
review of an empty change. The specialists must never fail the workflow -- a specialist
that times out contributes an incomplete result and the review continues. `gate` must
prefer handing a review to a human over losing it, so a failed auto-post degrades into
an approval request rather than a dropped review.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langgraph.types import Send

from backend.agents.base_agent import SpecialistInput
from backend.core.exceptions import ReviewSuperseded
from backend.database.repository import HitlRepository, ReviewRepository
from backend.database.session import session_scope
from backend.hitl.gate import apply_gate
from backend.hitl.publishing import publish_review
from backend.integrations.github_client import GitHubClient
from backend.memory.code_memory import CodeMemory
from backend.models.enums import (
    SPECIALIST_AGENTS,
    AgentType,
    EventType,
    Outcome,
    ReviewStatus,
)
from backend.models.review import ReviewRequest
from backend.observability.events import Span, emit_agent_event
from backend.orchestrator.aggregator import (
    merge_findings,
    overall_confidence,
    select_postable,
    severity_counts,
)
from backend.orchestrator.state import ReviewState, build_payload
from backend.settings import Settings, get_settings
from backend.tools.capability_scope import PUBLISHER_SCOPE

logger = logging.getLogger(__name__)


@dataclass
class NodeDependencies:
    github: GitHubClient
    agents: dict[AgentType, Any]
    memory: CodeMemory | None = None
    settings: Settings | None = None

    def resolved_settings(self) -> Settings:
        return self.settings or get_settings()


class ReviewNodes:
    """The node functions, bound to their dependencies.

    A class rather than four free functions so the dependencies are injected once and
    the graph wiring stays a list of method references.
    """

    def __init__(self, deps: NodeDependencies) -> None:
        self.deps = deps
        self.settings = deps.resolved_settings()

    # --- prepare -------------------------------------------------------------
    async def prepare(self, state: ReviewState) -> dict[str, Any]:
        """Fetch the PR, supersede older heads, retrieve context once for all passes."""
        review_id = state["review_id"]
        repo = state["repo"]
        pr_number = state["pr_number"]
        head_sha = state["head_sha"]
        errors: list[str] = []

        async with Span(
            str(AgentType.AGGREGATOR), review_id, note="prepare: fetch diff and context"
        ) as active:
            async with session_scope() as session:
                reviews = ReviewRepository(session)
                superseded = await reviews.supersede_previous_heads(repo, pr_number, head_sha)
                await reviews.set_status(review_id, ReviewStatus.RUNNING)
            if superseded:
                await active.event(
                    EventType.DECISION,
                    payload={"superseded_reviews": superseded},
                )

            pull_request = await self.deps.github.get_pull_request(repo, pr_number)
            errors.extend(pull_request.errors)

            await active.event(
                EventType.TOOL_CALL,
                payload={
                    "tool": "fetch_pull_request",
                    "source": pull_request.source,
                    "files": len(pull_request.changed_files),
                    "diff_chars": len(pull_request.diff),
                },
            )

            context: list[Any] = []
            if self.deps.memory is not None:
                try:
                    if await self.deps.memory.has_repo(repo):
                        query = _retrieval_query(pull_request)
                        context = await self.deps.memory.search(repo, query)
                        await active.event(
                            EventType.TOOL_CALL,
                            payload={
                                "tool": "retrieve_code_context",
                                "chunks": len(context),
                                "paths": sorted({c.path for c in context})[:20],
                            },
                        )
                    else:
                        errors.append(
                            f"repository '{repo}' is not indexed, so findings are ungrounded; "
                            f"run `pr-review index --path <repo> --name {repo}`"
                        )
                except Exception as exc:  # noqa: BLE001 - ungrounded beats unavailable
                    errors.append(f"retrieval failed: {exc}")
                    logger.warning("retrieval failed for %s: %s", repo, exc)

            if not pull_request.diff.strip():
                raise ValueError(
                    f"{repo}#{pr_number} has no diff available; refusing to review an empty change"
                )

            # Persist what is being reviewed. The console's diff view reads this
            # artifact, so it shows the code as it was at review time even after the
            # PR moves on. Errors here are degraded display, not a failed review.
            try:
                from backend.database.artifacts import ArtifactRepository
                from backend.settings import get_settings as _gs

                await ArtifactRepository(_gs().database_url).save(
                    review_id,
                    diff=pull_request.diff,
                    changed_files=list(pull_request.changed_files),
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"could not persist the diff artifact: {exc}")
                logger.warning("artifact persist failed for %s: %s", review_id, exc)

        return {
            "diff": pull_request.diff,
            "changed_files": pull_request.changed_files,
            "title": pull_request.title,
            "context": context,
            "errors": errors,
        }

    # --- dispatch ------------------------------------------------------------
    def dispatch(self, state: ReviewState) -> list[Send]:
        """Fan out one Send per specialist. This is the parallel fan-out (L3)."""
        agents = state.get("agents") or [a.value for a in SPECIALIST_AGENTS]
        if not agents:
            # Never return an empty list: with no sends the join never fires and the
            # workflow would end silently with no review.
            agents = [a.value for a in SPECIALIST_AGENTS]
        return [Send("specialist", build_payload(state, agent)) for agent in agents]

    # --- specialist ----------------------------------------------------------
    async def specialist(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one specialist. Never raises: a failed pass is an incomplete pass."""
        agent_type = AgentType(payload["agent"])
        agent = self.deps.agents[agent_type]
        inp = SpecialistInput(
            review_id=payload["review_id"],
            repo=payload["repo"],
            pr_number=payload["pr_number"],
            head_sha=payload["head_sha"],
            diff=payload.get("diff", ""),
            changed_files=payload.get("changed_files", []),
            title=payload.get("title"),
            context=payload.get("context", []),
        )
        try:
            result = await agent.run(inp)
        except Exception as exc:  # noqa: BLE001 - the join must always receive a value
            logger.exception("specialist %s failed unexpectedly", agent_type.value)
            from backend.models.findings import SpecialistResult

            result = SpecialistResult(
                agent_type=agent_type,
                errors=[f"{agent_type.value} pass failed: {exc}"],
                truncated=True,
            )

        return {
            "agent_findings": result.findings,
            "errors": [f"{agent_type.value}: {e}" for e in result.errors],
            "results": [result],
        }

    # --- aggregate -----------------------------------------------------------
    async def aggregate(self, state: ReviewState) -> dict[str, Any]:
        """Merge, deduplicate, score, and persist. Deterministic -- no LLM here."""
        review_id = state["review_id"]
        raw = state.get("agent_findings", [])
        results = state.get("results", [])
        expected = len(state.get("agents") or []) or 4

        async with Span(
            str(AgentType.AGGREGATOR), review_id, note="aggregate: merge and score"
        ) as active:
            merged = merge_findings(raw)
            truncated = sum(1 for r in results if getattr(r, "truncated", False))
            confidence = overall_confidence(
                merged.findings,
                completed_passes=len(results),
                total_passes=expected,
                truncated_passes=truncated,
            )
            counts = severity_counts(merged.findings)

            async with session_scope() as session:
                reviews = ReviewRepository(session)
                # Replace, not append: a re-driven or re-run review must not add its
                # findings beside a previous attempt's. Clearing here makes the
                # aggregator idempotent regardless of how the re-run was requested.
                cleared = await reviews.delete_findings(review_id)
                if cleared:
                    await active.event(
                        EventType.DECISION,
                        payload={"cleared_previous_findings": cleared},
                    )
                await reviews.store_findings(review_id, merged.findings)
                if merged.near_misses:
                    await reviews.record_near_misses(review_id, merged.near_misses)
                await reviews.set_status(
                    review_id, ReviewStatus.RUNNING, overall_confidence=confidence
                )

            total_cost = round(sum(getattr(r, "cost_usd", 0.0) for r in results), 6)
            await active.event(
                EventType.DECISION,
                confidence=confidence,
                cost_usd=total_cost,
                payload={
                    "raw_findings": len(raw),
                    "merged_findings": len(merged.findings),
                    "duplicates_collapsed": merged.duplicates_collapsed,
                    "near_misses": len(merged.near_misses),
                    "severity_counts": counts,
                    "incomplete_passes": truncated,
                    "passes_completed": len(results),
                    "passes_expected": expected,
                },
            )
            active.confidence = confidence

        return {
            "findings": merged.findings,
            "overall_confidence": confidence,
            "severity_counts": counts,
        }

    # --- gate ----------------------------------------------------------------
    async def gate(self, state: ReviewState) -> dict[str, Any]:
        """Route: hand to a human, or publish when policy allows."""
        review_id = state["review_id"]
        findings = state.get("findings", [])
        confidence = float(state.get("overall_confidence") or 0.0)
        result = apply_gate(findings, confidence, self.settings)

        async with Span(
            str(AgentType.AGGREGATOR), review_id, note=f"gate: {result.decision.value}"
        ) as active:
            if result.requires_human:
                await active.event(
                    EventType.DECISION,
                    confidence=confidence,
                    outcome=Outcome.ESCALATED.value if result.escalates else None,
                    payload=result.as_event_payload(),
                )
                async with session_scope() as session:
                    hitl = HitlRepository(session)
                    await hitl.enqueue(review_id, result.reason)
                    reviews = ReviewRepository(session)
                    await reviews.set_status(
                        review_id,
                        ReviewStatus.PENDING_APPROVAL,
                        decision=result.decision.value,
                        overall_confidence=confidence,
                        hitl_mode=result.hitl_mode,
                    )
                if result.escalates:
                    await emit_agent_event(
                        agent=str(AgentType.AGGREGATOR),
                        event_type=EventType.ESCALATION,
                        review_id=review_id,
                        outcome=Outcome.CRITICAL_BLOCK.value,
                        confidence=confidence,
                        payload={
                            "critical": len(result.critical_findings),
                            "locations": [f.locator for f in result.critical_findings][:10],
                            "reason": result.reason,
                        },
                    )
                return {
                    "decision": result.decision.value,
                    "gate_reason": result.reason,
                    "posted": False,
                }

            # Auto-post is off by default (HITL_MODE=always), so this path is the
            # opt-in one. `publish_review` re-checks that the commit is still current.
            postable = select_postable(findings, max_comments=self.settings.max_comments_per_review)
            try:
                published = await publish_review(
                    review_id=review_id,
                    repo=state["repo"],
                    pr_number=state["pr_number"],
                    head_sha=state["head_sha"],
                    findings=postable,
                    actor="agent (auto-post)",
                    github=self.deps.github,
                    overall_confidence=confidence,
                    agent_errors=state.get("errors", []),
                    incomplete_passes=sum(
                        1 for r in state.get("results", []) if getattr(r, "truncated", False)
                    ),
                    cost_usd=round(
                        sum(getattr(r, "cost_usd", 0.0) for r in state.get("results", [])), 6
                    ),
                    scope=PUBLISHER_SCOPE,
                    settings=self.settings,
                )
            except ReviewSuperseded as exc:
                logger.info("not posting: %s", exc)
                async with session_scope() as session:
                    await ReviewRepository(session).set_status(
                        review_id, ReviewStatus.SUPERSEDED, error=str(exc)
                    )
                return {
                    "decision": "superseded",
                    "gate_reason": str(exc),
                    "posted": False,
                }
            except Exception as exc:  # noqa: BLE001 - a failed post becomes a human's job
                logger.exception("auto-post failed; routing to a human instead")
                reason = f"auto-post failed, needs a human: {exc}"
                async with session_scope() as session:
                    await HitlRepository(session).enqueue(review_id, reason)
                    await ReviewRepository(session).set_status(
                        review_id,
                        ReviewStatus.PENDING_APPROVAL,
                        decision="human_approval",
                        overall_confidence=confidence,
                        hitl_mode=result.hitl_mode,
                        error=str(exc),
                    )
                return {
                    "decision": "human_approval",
                    "gate_reason": reason,
                    "posted": False,
                }

            return {
                "decision": result.decision.value,
                "gate_reason": result.reason,
                "posted": True,
                "github_review_url": published.github_review_url,
            }


def _retrieval_query(pull_request: Any) -> str:
    from backend.tools.diffparse import parse_added_lines

    added = parse_added_lines(pull_request.diff)
    body = "\n".join(line.text for line in added[:300]) if added else pull_request.diff[:4000]
    paths = " ".join(f.path for f in pull_request.changed_files[:10])
    return f"{paths}\n{body}".strip()[:4000]


def review_request_from_state(state: ReviewState) -> ReviewRequest:
    return ReviewRequest(
        review_id=state["review_id"],
        repo=state["repo"],
        pr_number=state["pr_number"],
        head_sha=state["head_sha"],
        delivery_id=state.get("delivery_id"),
    )


__all__ = ["NodeDependencies", "ReviewNodes", "review_request_from_state"]
