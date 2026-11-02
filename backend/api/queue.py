"""Queue status: how much work is waiting, and how much of it is a human's."""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.api.deps import get_app_context
from backend.hitl.queue import queue_depth
from backend.job_queue.queue import queue_health

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/status")
async def status(request: Request) -> dict[str, object]:
    context = get_app_context(request)
    depth = await queue_depth()
    return {
        "redis": await queue_health(context.settings),
        "reviews": {
            "queued": depth.queued,
            "running": depth.running,
            "pending_approval": depth.pending_approval,
            "oldest_pending_seconds": depth.oldest_pending_seconds,
        },
        "hitl_mode": context.settings.hitl_mode,
        "max_comments_per_review": context.settings.max_comments_per_review,
    }
