"""Explainability: turn a finding back into the evidence that produced it.

When a developer disputes "this endpoint is vulnerable to SQL injection", the
system has to answer three questions from the spine alone, with no re-running:

    Which model and prompt version raised it?
    What did that pass cost and how long did it take?
    Did any other specialist independently see the same thing?

If it cannot answer those, the finding is an assertion rather than a review.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.enums import EventType
from backend.models.findings import Finding
from backend.models.review import AgentEvent


@dataclass
class FindingExplanation:
    finding: Finding
    model: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    cost_usd: float = 0.0
    latency_ms: int | None = None
    agreeing_agents: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    complete: bool = False

    @property
    def caveat(self) -> str | None:
        if self.complete:
            return None
        return (
            "This pass did not record the model and prompt version that produced the "
            "finding, so it cannot be fully reproduced. Treat it as unverified."
        )


def explain_finding(finding: Finding, events: list[AgentEvent]) -> FindingExplanation:
    """Rebuild a finding's provenance from the review's event stream."""
    explanation = FindingExplanation(finding=finding)
    agent = str(finding.agent_type)

    for event in events:
        if str(event.agent) != agent:
            continue
        if str(event.event_type) == EventType.LLM_CALL.value:
            explanation.model = event.model or explanation.model
            if event.cost_usd:
                explanation.cost_usd += float(event.cost_usd)
            if event.latency_ms is not None:
                explanation.latency_ms = event.latency_ms
            payload = event.payload or {}
            explanation.prompt_version = payload.get("prompt_version") or explanation.prompt_version
            explanation.prompt_hash = payload.get("prompt_hash") or explanation.prompt_hash
            retrieved = payload.get("retrieved_chunks")
            if retrieved:
                explanation.evidence.append(f"grounded in {retrieved} retrieved code chunk(s)")
        elif str(event.event_type) == EventType.SPAN_END.value:
            payload = event.payload or {}
            if payload.get("retrieved_paths"):
                paths = ", ".join(payload["retrieved_paths"][:5])
                explanation.evidence.append(f"context paths: {paths}")

    explanation.agreeing_agents = [str(a) for a in finding.agreed_by]
    if explanation.agreeing_agents:
        explanation.evidence.append(
            "independently raised by: " + ", ".join(explanation.agreeing_agents)
        )
    explanation.complete = explanation.model is not None and explanation.prompt_hash is not None
    return explanation


def explain_review(findings: list[Finding], events: list[AgentEvent]) -> list[FindingExplanation]:
    return [explain_finding(f, events) for f in findings]


def run_summary(events: list[AgentEvent]) -> tuple[list[str], int]:
    """Warnings and incomplete-pass count for a run, derived from the spine alone.

    Single implementation because three callers need it -- the review detail page, the
    publication body and the decision audit -- and a review that reports different
    warnings depending on which surface you look at is worse than no warnings.
    """
    errors: list[str] = []
    incomplete = 0
    for event in events:
        kind = str(event.event_type)
        if kind == EventType.SPAN_END.value and event.outcome == "incomplete":
            incomplete += 1
            errors.append(f"{event.agent} pass ended incomplete")
        elif kind == EventType.LLM_CALL.value and event.outcome == "error":
            detail = (event.payload or {}).get("error", "llm error")
            errors.append(f"{event.agent}: {detail}")
    return errors, incomplete
