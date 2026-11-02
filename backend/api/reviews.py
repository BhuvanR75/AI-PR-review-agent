"""Review reads and the user-initiated trigger.

The list the console opens on, one review in full (with the diff artifact), its
trace, and the POST that starts a review from the console.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from backend.api.deps import get_app_context, get_hitl_service
from backend.api.schemas import detail_from, summary_from_row
from backend.bootstrap import AppContext
from backend.core.exceptions import ReviewNotFound
from backend.database.artifacts import ArtifactRepository
from backend.database.pg import get_pool
from backend.database.repository import HitlRepository, ReviewRepository
from backend.database.session import session_scope
from backend.economics.cost_repository import CostRepository
from backend.hitl.decisions import HitlService
from backend.models.enums import HitlState, ReviewStatus
from backend.models.review import ReviewDetail, ReviewSummary, TraceResponse
from backend.observability.audit import explain_review
from backend.observability.events import read_trace

logger = logging.getLogger(__name__)


class TriggerRequest(BaseModel):
    repo: str = Field(pattern=r"^[\w.-]+/[\w.-]+$")
    pr_number: int = Field(ge=1)


class TriggerResponse(BaseModel):
    review_id: str
    status: str
    created: bool
    message: str

router = APIRouter(prefix="/api/reviews", tags=["reviews"])

ACTIVE_STATUSES = [
    ReviewStatus.PENDING_APPROVAL,
    ReviewStatus.QUEUED,
    ReviewStatus.RUNNING,
]


@router.get("", response_model=list[ReviewSummary])
async def list_reviews(
    request: Request,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ReviewSummary]:
    statuses: list[ReviewStatus] | None = None
    if status_filter == "active":
        statuses = ACTIVE_STATUSES
    elif status_filter == "pending":
        statuses = [ReviewStatus.PENDING_APPROVAL]
    elif status_filter:
        try:
            statuses = [ReviewStatus(status_filter)]
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown status '{status_filter}'",
            ) from exc

    context = get_app_context(request)
    costs = CostRepository(get_pool(), context.settings)

    async with session_scope() as session:
        rows = await ReviewRepository(session).list_rows(
            statuses=statuses, limit=limit, offset=offset
        )
    cost_map = await costs.costs_for_reviews([str(r.review.id) for r in rows])
    return [summary_from_row(row, cost_map.get(str(row.review.id))) for row in rows]


@router.post("/trigger", response_model=TriggerResponse)
async def trigger_review(
    request: Request,
    payload: TriggerRequest,
    context: Annotated[AppContext, Depends(get_app_context)],
) -> TriggerResponse:
    """Start a review of one open PR, from the console.

    The PR metadata and diff are fetched here, at trigger time, and shipped inside the
    job payload -- so the worker needs no credentials and no re-fetch, and what gets
    reviewed is exactly what you were looking at when you pressed the button.
    """
    from backend.integrations.connection_store import ConnectionStore
    from backend.integrations.github_client import LiveGitHubClient
    from backend.job_queue.queue import enqueue_review

    settings = context.settings

    # Resolve the connection at trigger time: the same dynamic resolution the
    # resolver applies, but here we can refuse with a clear message when there is
    # nothing live to review with.
    connection = await ConnectionStore(settings.database_url).get_active()
    client: Any
    if connection is not None:
        client = LiveGitHubClient(settings, token=connection.token)
    elif settings.github_client_mode == "live":
        client = LiveGitHubClient(settings)
    else:
        client = context.github  # dry-run: the demo PR, so the flow is walkable offline

    try:
        info = await client.get_pull_request(payload.repo, payload.pr_number)
    except Exception as exc:  # noqa: BLE001 - surfaced as 502 with the provider's words
        raise HTTPException(
            status_code=502,
            detail=f"could not read {payload.repo}#{payload.pr_number}: {exc}",
        ) from exc

    if not info.diff.strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "that pull request has no readable diff "
                "(it may be unmergeable or empty); nothing to review"
            ),
        )

    # One review per (repo, pr, head_sha), shared with the webhook path: re-pressing
    # the button, or a webhook for the same commit, land on the same row.
    from backend.models.webhook import WebhookEvent

    event = WebhookEvent(
        delivery_id=f"trigger-{uuid.uuid4()}",
        event_type="pull_request",
        action="opened",
        repo=info.repo,
        pr_number=info.number,
        head_sha=info.head_sha,
        base_sha=info.base_sha,
        title=info.title,
        author=info.author,
        is_draft=info.draft,
    )
    async with session_scope() as session:
        upsert = await ReviewRepository(session).upsert_review_from_event(event)
        review_id = str(upsert.review.id)

    await ArtifactRepository(settings.database_url).save(
        review_id,
        diff=info.diff,
        changed_files=info.changed_files,
    )

    if not (upsert.created or upsert.redrivable):
        # Already decided and not re-drivable: no new job, and the response says so.
        logger.info(
            "trigger for an already-decided review",
            extra={"context": {"review_id": review_id, "status": upsert.review.status}},
        )
    else:
        # A review row that already existed may have a retained ARQ result from a
        # previous attempt; redriving needs a fresh job id to actually run, and a
        # fresh checkpoint thread so the rerun does not append to the old attempt's
        # accumulated state.
        await enqueue_review(
            review_id=review_id,
            repo=info.repo,
            pr_number=info.number,
            head_sha=info.head_sha,
            settings=settings,
            redrive=not upsert.created,
            thread_id=f"{review_id}:{uuid.uuid4().hex[:8]}" if not upsert.created else None,
        )

    logger.info(
        "review triggered from console",
        extra={
            "context": {
                "review_id": review_id,
                "repo": payload.repo,
                "pr": payload.pr_number,
                "created": upsert.created,
                "redrivable": upsert.redrivable,
            }
        },
    )
    return TriggerResponse(
        review_id=review_id,
        status=upsert.review.status,
        created=upsert.created,
        message=(
            f"{info.repo}#{info.number} — {len(info.changed_files)} file(s), "
            f"{len(info.diff)} diff chars — "
            + (
                "review queued"
                if upsert.created or upsert.redrivable
                else "already reviewed and decided; showing that review"
            )
        ),
    )


@router.get("/{review_id}", response_model=ReviewDetail)
async def get_review(request: Request, review_id: str) -> ReviewDetail:
    context = get_app_context(request)
    costs = CostRepository(get_pool(), context.settings)

    async with session_scope() as session:
        reviews = ReviewRepository(session)
        hitl = HitlRepository(session)
        review = await reviews.get(review_id)
        if review is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="review not found")
        stored = await reviews.list_findings(review_id)
        record = await hitl.get_for_review(review_id)

    artifact = await ArtifactRepository(context.settings.database_url).get(review_id)
    events = await read_trace(review_id)
    return detail_from(
        review,
        stored,
        hitl_state=HitlState(record.state) if record else None,
        cost=await costs.review_cost(review_id),
        events=events,
        artifact=artifact,
    )


@router.get("/{review_id}/trace", response_model=TraceResponse)
async def get_trace(request: Request, review_id: str) -> TraceResponse:
    """Reconstruct any review end to end: one indexed query on the events spine."""
    context = get_app_context(request)
    events = await read_trace(review_id)
    cost = await CostRepository(get_pool(), context.settings).review_cost(review_id)
    return TraceResponse(
        review_id=review_id,
        events=events,
        cost=cost,
        total_events=len(events),
    )


@router.get("/{review_id}/explain")
async def explain(
    request: Request,
    review_id: str,
    service: Annotated[HitlService, Depends(get_hitl_service)],
) -> dict[str, object]:
    """Per-finding provenance: which model, which prompt version, what it cost.

    This is the endpoint a developer disputes a finding through, and the reason the
    prompt hash is on the spine. If it cannot name the prompt and the model, it says so
    rather than implying the finding is reproducible.
    """
    try:
        _review, stored, _state = await service.get_review_and_findings(review_id)
    except ReviewNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    events = await read_trace(review_id)
    explanations = explain_review([item.finding for item in stored], events)
    return {
        "review_id": review_id,
        "explanations": [
            {
                "locator": e.finding.locator,
                "agent": e.finding.agent_type.value,
                "severity": e.finding.severity.value,
                "model": e.model,
                "prompt_version": e.prompt_version,
                "prompt_hash": e.prompt_hash,
                "cost_usd": round(e.cost_usd, 6),
                "latency_ms": e.latency_ms,
                "agreeing_agents": e.agreeing_agents,
                "evidence": e.evidence,
                "reproducible": e.complete,
                "caveat": e.caveat,
            }
            for e in explanations
        ],
    }
