"""Review-level contracts: what the API returns and what the console renders."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.models.enums import (
    AgentType,
    Decision,
    EventType,
    HitlState,
    ReviewStatus,
)
from backend.models.findings import Finding


class ChangedFile(BaseModel):
    path: str
    status: str = "modified"
    additions: int = 0
    deletions: int = 0


class ReviewCost(BaseModel):
    """Attributed from the events spine, not tracked in a side-channel."""

    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    by_agent: dict[str, float] = Field(default_factory=dict)


class ReviewSummary(BaseModel):
    id: str
    repo: str
    pr_number: int
    title: str | None = None
    author: str | None = None
    head_sha: str
    status: ReviewStatus
    decision: Decision | None = None
    hitl_state: HitlState | None = None
    overall_confidence: float | None = None
    finding_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    cost: ReviewCost = Field(default_factory=ReviewCost)
    created_at: datetime
    updated_at: datetime


class ReviewDetail(ReviewSummary):
    findings: list[Finding] = Field(default_factory=list)
    #: Database identities for the findings above, positionally aligned with them.
    #: Kept separate on purpose: a Finding is a value that travels through the
    #: aggregator, and only a human decision needs the row identity behind it. The
    #: approval endpoint takes these ids when a reviewer posts a subset.
    finding_ids: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    github_review_url: str | None = None
    hitl_notes: str | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    #: The reviewed diff, persisted at review time. None for reviews that ran before
    #: artifacts existed; the console degrades to findings-only in that case.
    diff: str | None = None


class AgentEvent(BaseModel):
    """One row of the time-ordered spine (L6)."""

    ts: datetime
    review_id: str | None = None
    agent: AgentType | str
    span_id: str
    parent_span: str | None = None
    event_type: EventType | str
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    outcome: str | None = None
    confidence: float | None = None
    payload: dict | None = None


class TraceResponse(BaseModel):
    review_id: str
    events: list[AgentEvent]
    cost: ReviewCost
    total_events: int = 0


class ReviewRequest(BaseModel):
    """The internal request the workflow engine receives."""

    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    delivery_id: str | None = None


class ReviewOutcome(BaseModel):
    review_id: str
    status: ReviewStatus
    decision: Decision | None = None
    overall_confidence: float | None = None
    finding_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    critical_findings: list[Finding] = Field(default_factory=list)


class HitlDecisionRequest(BaseModel):
    actor: str = "unknown"
    note: str | None = None
    finding_ids: list[str] | None = Field(
        default=None,
        description="Approve-only-these findings. None means every finding. Never for reject.",
    )


class HitlRejectRequest(BaseModel):
    actor: str = "unknown"
    reason: str = Field(min_length=1)


class HitlRerunRequest(BaseModel):
    actor: str = "unknown"
    note: str | None = None
    agents: list[AgentType] | None = Field(
        default=None, description="Limit the rerun to these specialists. None means all four."
    )


class HitlActionResponse(BaseModel):
    review_id: str
    status: ReviewStatus
    hitl_state: HitlState
    posted_finding_ids: list[str] = Field(default_factory=list)
    github_review_url: str | None = None
    message: str


class QueueDepth(BaseModel):
    queued: int = 0
    running: int = 0
    pending_approval: int = 0
    oldest_pending_seconds: float | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    has_timescale: bool = False
    has_vectorscale: bool = False
