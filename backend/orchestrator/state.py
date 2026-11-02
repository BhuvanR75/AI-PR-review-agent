"""Typed workflow state.

Two reducers, and both are load-bearing:

  * `findings` and `errors` accumulate with `operator.add`, because four specialists
    write them in parallel and the aggregator must see all four lists. This is also
    what makes the join work: the aggregator runs once, after every branch in the
    super-step has contributed.
  * everything else is replace-on-write, so `prepare` can hand shared context (the
    diff, the retrieved chunks) to all four passes without each one re-fetching it.

The state is checkpointed, so everything in it must be serializable -- see
`serde.py` for the allow-list that declares which of our types may appear.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from backend.memory.context_retriever import RetrievedChunk
from backend.models.enums import SPECIALIST_AGENTS
from backend.models.findings import Finding, SpecialistResult
from backend.models.review import ChangedFile, ReviewRequest


class ReviewState(TypedDict, total=False):
    """What flows between the nodes."""

    # identity
    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    title: str | None
    delivery_id: str | None

    # shared context, fetched once by prepare
    diff: str
    changed_files: list[ChangedFile]
    context: list[RetrievedChunk]

    # which passes to run (a re-run may target a subset)
    agents: list[str]

    # Accumulated across the fan-out. `agent_findings` is the raw union of what the
    # specialists returned; `findings` is the merged, deduplicated, canonical list the
    # aggregator writes. They are separate keys on purpose -- a single `findings` key
    # with an add-reducer would double the list the moment the aggregator returned it.
    agent_findings: Annotated[list[Finding], operator.add]
    errors: Annotated[list[str], operator.add]
    results: Annotated[list[SpecialistResult], operator.add]

    # decided by the aggregator (replace-on-write)
    findings: list[Finding]
    overall_confidence: float
    severity_counts: dict[str, int]
    decision: str | None
    gate_reason: str | None
    posted: bool
    github_review_url: str | None


class SpecialistPayload(TypedDict, total=False):
    """The `Send` argument one specialist node receives.

    It carries the shared context rather than reading it from the parent state,
    because a Send argument is the node's input in its entirety.
    """

    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    title: str | None
    agent: str
    diff: str
    changed_files: list[ChangedFile]
    context: list[RetrievedChunk]


def initial_state(request: ReviewRequest, *, agents: list[str] | None = None) -> ReviewState:
    return ReviewState(
        review_id=request.review_id,
        repo=request.repo,
        pr_number=request.pr_number,
        head_sha=request.head_sha,
        delivery_id=request.delivery_id,
        agents=agents or [a.value for a in SPECIALIST_AGENTS],
        agent_findings=[],
        errors=[],
        results=[],
    )


def build_payload(state: ReviewState, agent: str) -> SpecialistPayload:
    """One specialist's input, sharing the context prepare already fetched."""
    payload = SpecialistPayload(
        review_id=state["review_id"],
        repo=state["repo"],
        pr_number=state["pr_number"],
        head_sha=state["head_sha"],
        agent=agent,
        diff=state.get("diff", ""),
        changed_files=state.get("changed_files", []),
        context=state.get("context", []),
    )
    if state.get("title"):
        payload["title"] = state["title"]
    return payload


def state_as_dict(state: ReviewState | dict[str, Any]) -> dict[str, Any]:
    return dict(state)
