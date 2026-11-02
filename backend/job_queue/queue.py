"""The ARQ producer side.

Ingress acknowledges GitHub and enqueues; the worker does the work. That decoupling is
correctness, not just performance -- a slow model provider must not make the webhook
endpoint time out and cause GitHub to mark the delivery as failed.

The job id is derived from the review and the commit, so a duplicate enqueue of the
same review for the same commit is refused by Redis rather than by application logic.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)

REVIEW_JOB = "run_review"

_pool: ArqRedis | None = None


def redis_settings(settings: Settings | None = None) -> RedisSettings:
    resolved = settings or get_settings()
    return RedisSettings.from_dsn(resolved.redis_url)


async def get_queue(settings: Settings | None = None) -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(redis_settings(settings))
    return _pool


async def close_queue() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


def job_id_for(review_id: str, head_sha: str) -> str:
    return f"review:{review_id}:{head_sha[:12]}"


async def enqueue_review(
    *,
    review_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    delivery_id: str | None = None,
    settings: Settings | None = None,
    redrive: bool = False,
    thread_id: str | None = None,
) -> str | None:
    """Enqueue a review. Returns the job id, or None if one was already queued.

    `redrive=True` is for re-driving a review that already exists (failed, or stuck
    in a redrivable state): it suffixes the job id with a nonce so ARQ cannot
    silently drop the job because a previous attempt's result is still retained.
    The standard id is kept for fresh reviews and webhook deliveries, where dropping
    a duplicate of the same commit is exactly what we want.
    """
    queue = await get_queue(settings)
    job_id = job_id_for(review_id, head_sha)
    if redrive:
        job_id = f"{job_id}:{uuid.uuid4().hex[:8]}"
    job = await queue.enqueue_job(
        REVIEW_JOB,
        review_id,
        repo,
        pr_number,
        head_sha,
        delivery_id,
        thread_id,
        _job_id=job_id,
    )
    if job is None:
        logger.info(
            "review already queued for this commit",
            extra={"context": {"review_id": review_id, "head_sha": head_sha[:8]}},
        )
        return None
    return job.job_id


async def queue_health(settings: Settings | None = None) -> dict[str, Any]:
    try:
        queue = await get_queue(settings)
        await queue.ping()
        queued = await queue.zcard("arq:queue")
        return {"connected": True, "queued_jobs": int(queued or 0)}
    except Exception as exc:  # noqa: BLE001 - readiness must never raise
        return {"connected": False, "error": str(exc), "queued_jobs": 0}
