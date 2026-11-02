"""The worker: pulls a review job and drives the workflow engine.

This is also the only place that decides what a *failed* review looks like. A workflow
that raises must leave the review row in `failed` with the error recorded, because a
review stuck in `running` is invisible -- it never reaches a human, and nobody is
alerted. Failing visibly is the requirement.

Cron jobs run the alert scan (the escalation-rate and drift checks) and, when
TimescaleDB is absent, refresh the plain materialized views instead.
"""

from __future__ import annotations

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from backend.bootstrap import AppContext, build_context, shutdown_context
from backend.core.exceptions import ReviewSuperseded, WorkflowError
from backend.database.repository import ReviewRepository
from backend.database.session import session_scope
from backend.models.enums import ReviewStatus
from backend.models.review import ReviewRequest
from backend.observability.alerting import collect_alerts
from backend.observability.logging import configure_logging
from backend.observability.tracing import bind_review
from backend.reliability.idempotency import mark_delivery
from backend.settings import get_settings

logger = logging.getLogger(__name__)

APP_KEY = "app"


async def process_review(
    context: AppContext, request: ReviewRequest, *, thread_id: str | None = None
) -> dict[str, Any]:
    """Run one review to completion. Shared by the worker job and the tests, so the
    tested path is the production path."""
    try:
        with bind_review(request.review_id):
            outcome = await context.engine.run(request, thread_id=thread_id)
    except ReviewSuperseded:
        logger.info("review superseded before it ran", extra={"context": {"id": request.review_id}})
        await _set_status(request.review_id, ReviewStatus.SUPERSEDED)
        return {"review_id": request.review_id, "status": ReviewStatus.SUPERSEDED.value}
    except WorkflowError as exc:
        logger.exception("review workflow failed")
        await _set_status(request.review_id, ReviewStatus.FAILED, error=str(exc))
        return {
            "review_id": request.review_id,
            "status": ReviewStatus.FAILED.value,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 - never leave a review stuck in `running`
        logger.exception("unexpected failure running review")
        await _set_status(request.review_id, ReviewStatus.FAILED, error=str(exc))
        return {
            "review_id": request.review_id,
            "status": ReviewStatus.FAILED.value,
            "error": str(exc),
        }

    return {
        "review_id": outcome.review_id,
        "status": outcome.status.value,
        "findings": outcome.finding_count,
        "severity_counts": outcome.severity_counts,
        "confidence": outcome.overall_confidence,
        "cost_usd": outcome.cost_usd,
        "errors": outcome.errors,
    }


async def _set_status(review_id: str, status: ReviewStatus, error: str | None = None) -> None:
    try:
        async with session_scope() as session:
            await ReviewRepository(session).set_status(review_id, status, error=error)
    except Exception:  # noqa: BLE001 - failing to record a failure must not raise
        logger.exception("could not record status %s for %s", status, review_id)


async def run_review(
    ctx: dict[Any, Any],
    review_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    delivery_id: str | None = None,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """The ARQ job. Its arguments are primitives so the job survives the Redis round trip."""
    context: AppContext = ctx[APP_KEY]
    request = ReviewRequest(
        review_id=review_id,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        delivery_id=delivery_id,
    )
    result = await process_review(context, request, thread_id=thread_id)
    if delivery_id:
        try:
            await mark_delivery(delivery_id, result.get("status", "done"))
        except Exception:  # noqa: BLE001
            logger.warning("could not update delivery %s", delivery_id)
    return result


async def scan_alerts(ctx: dict[Any, Any]) -> dict[str, Any]:
    """The monitoring pass: queue age, drift, cost ceiling, audit gaps."""
    alerts = await collect_alerts(get_settings())
    for alert in alerts:
        logger.warning(
            "alert",
            extra={
                "context": {"kind": alert.kind, "severity": alert.severity.value, **alert.context}
            },
        )
    return {"alerts": [a.as_dict() for a in alerts]}


async def startup(ctx: dict[Any, Any]) -> None:
    configure_logging()
    ctx[APP_KEY] = await build_context()
    logger.info("worker started", extra={"context": ctx[APP_KEY].describe()})


async def shutdown(ctx: dict[Any, Any]) -> None:
    await shutdown_context(ctx.get(APP_KEY))
    logger.info("worker stopped")


class WorkerSettings:
    """ARQ entry point: `arq backend.job_queue.arq_worker.WorkerSettings`."""

    functions = [run_review]
    cron_jobs = [
        # The human-bottleneck and drift checks, every five minutes.
        cron(scan_alerts, minute=set(range(0, 60, 5)), run_at_startup=True, timeout=120),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 8
    job_timeout = 900
    keep_result = 3600
    max_tries = 2
    health_check_interval = 30
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
