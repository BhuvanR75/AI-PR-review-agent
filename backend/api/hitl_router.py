"""The approval endpoints. These are what the console and the CLI both call.

The response carries what actually happened (which findings were posted, how many
comments, whether it was a dry run) rather than only a status code, because "approved"
without "and here is what was published" is not something a human can verify.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from backend.api.deps import get_hitl_service
from backend.core.exceptions import (
    GitHubError,
    InvalidReviewState,
    ReviewNotFound,
    ReviewSuperseded,
)
from backend.hitl.decisions import DecisionOutcome, HitlService
from backend.hitl.queue import pending_rows, queue_depth
from backend.models.review import (
    HitlActionResponse,
    HitlDecisionRequest,
    HitlRejectRequest,
    HitlRerunRequest,
    QueueDepth,
    ReviewSummary,
)

router = APIRouter(prefix="/api/hitl", tags=["hitl"])

ServiceDep = Annotated[HitlService, Depends(get_hitl_service)]


def _respond(outcome: DecisionOutcome) -> HitlActionResponse:
    return HitlActionResponse(
        review_id=outcome.review_id,
        status=outcome.status,
        hitl_state=outcome.hitl_state,
        posted_finding_ids=outcome.posted_finding_ids,
        github_review_url=outcome.github_review_url,
        message=(
            f"{outcome.message}"
            + (" (dry run: nothing was sent to GitHub)" if outcome.dry_run else "")
        ),
    )


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, ReviewNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, ReviewSuperseded):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{exc} -- a newer commit exists, so this review must not be posted",
        )
    if isinstance(exc, InvalidReviewState):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, GitHubError):
        # GitHub refused the post (token scope, archived repo, lock...). The console
        # and the human deciding both need GitHub's actual reason, not a bare 500.
        hint = (
            "If this is 'Resource not accessible by personal access token', the token "
            "is missing the 'Pull requests: Read and write' permission -- grant it, "
            "then reconnect."
            if (exc.status_code == 403 and "not accessible" in str(exc))
            else None
        )
        detail = f"GitHub refused the post: {exc}" + (f" -- {hint}" if hint else "")
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    raise exc


@router.get("/queue", response_model=QueueDepth)
async def get_queue_depth() -> QueueDepth:
    return await queue_depth()


@router.get("/pending", response_model=list[ReviewSummary])
async def list_pending(limit: int = 50, offset: int = 0) -> list[ReviewSummary]:
    from backend.api.schemas import summary_from_row

    rows = await pending_rows(limit=limit, offset=offset)
    return [summary_from_row(row) for row in rows]


@router.post("/{review_id}/approve", response_model=HitlActionResponse)
async def approve(
    review_id: str,
    payload: HitlDecisionRequest,
    service: ServiceDep,
) -> HitlActionResponse:
    try:
        outcome = await service.approve(
            review_id,
            actor=payload.actor,
            note=payload.note,
            finding_ids=payload.finding_ids,
        )
    except (ReviewNotFound, ReviewSuperseded, InvalidReviewState) as exc:
        raise _translate(exc) from exc
    return _respond(outcome)


@router.post("/{review_id}/reject", response_model=HitlActionResponse)
async def reject(
    review_id: str,
    payload: HitlRejectRequest,
    service: ServiceDep,
) -> HitlActionResponse:
    try:
        outcome = await service.reject(review_id, actor=payload.actor, reason=payload.reason)
    except (ReviewNotFound, ReviewSuperseded, InvalidReviewState) as exc:
        raise _translate(exc) from exc
    return _respond(outcome)


@router.post("/{review_id}/rerun", response_model=HitlActionResponse)
async def rerun(
    review_id: str,
    payload: HitlRerunRequest,
    service: ServiceDep,
) -> HitlActionResponse:
    try:
        outcome = await service.rerun(
            review_id, actor=payload.actor, note=payload.note, agents=payload.agents
        )
    except (ReviewNotFound, ReviewSuperseded, InvalidReviewState) as exc:
        raise _translate(exc) from exc
    return _respond(outcome)
