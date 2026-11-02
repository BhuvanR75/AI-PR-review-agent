"""Ingress. The end of the system that must always answer quickly.

Order of operations, each one a defense that has to happen before the next:

  1. Read the raw body and verify the HMAC over those exact bytes.
  2. Check the required headers.
  3. Drop event types we do not review (a `pull_request` webhook fires for `labeled`
     and `edited` too).
  4. Reserve the delivery id -- a conditional insert, so concurrent redelivery cannot
     both win.
  5. Resolve the review row, then enqueue.
  6. Return 202 immediately.

Steps 4 and 5 return 503 when their dependency is unreachable rather than swallowing
the failure, because a 503 makes GitHub redeliver and the idempotency key makes the
redelivery safe. Accepting a delivery we did not enqueue would silently lose the PR.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from backend.api.deps import get_app_context
from backend.database.repository import ReviewRepository
from backend.database.session import session_scope
from backend.job_queue.queue import enqueue_review
from backend.reliability.idempotency import mark_delivery, reserve_delivery
from backend.webhook_receiver.parser import (
    PayloadError,
    parse_pull_request_event,
    skip_reason,
    summarize,
)
from backend.webhook_receiver.validator import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    SignatureError,
    require_headers,
    verify_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

REVIEWABLE_EVENT = "pull_request"


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(request: Request) -> dict[str, object]:
    body = await request.body()
    context = get_app_context(request)
    secret = context.settings.github_webhook_secret

    try:
        verify_signature(
            secret=secret, body=body, signature_header=request.headers.get(SIGNATURE_HEADER)
        )
        delivery_id, event_type = require_headers(
            delivery_id=request.headers.get(DELIVERY_HEADER),
            event_type=request.headers.get(EVENT_HEADER),
        )
    except SignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

    if event_type != REVIEWABLE_EVENT:
        return {"status": "ignored", "reason": f"event type '{event_type}' is not reviewed"}

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid JSON") from exc

    try:
        event = parse_pull_request_event(payload, delivery_id=delivery_id)
    except PayloadError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    reason = skip_reason(event)
    if reason is not None:
        logger.info("skipping webhook: %s", reason, extra={"context": {"delivery": delivery_id}})
        try:
            await reserve_delivery(
                delivery_id=delivery_id,
                event_type=event_type,
                action=event.action,
                repo=event.repo,
                pr_number=event.pr_number,
            )
            await mark_delivery(delivery_id, "skipped", reason=reason)
        except Exception:  # noqa: BLE001 - skipping must not fail the response
            logger.warning("could not record skipped delivery %s", delivery_id)
        return {"status": "ignored", "reason": reason}

    # 4. idempotency
    try:
        reserved = await reserve_delivery(
            delivery_id=delivery_id,
            event_type=event_type,
            action=event.action,
            repo=event.repo,
            pr_number=event.pr_number,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("idempotency store unavailable: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="idempotency store unavailable; redeliver",
        ) from exc

    if not reserved:
        return {"status": "duplicate", "delivery_id": delivery_id}

    # 5. resolve or reuse the review row
    try:
        async with session_scope() as session:
            upsert = await ReviewRepository(session).upsert_review_from_event(event)
            review_id = str(upsert.review.id)
            created, redrivable = upsert.created, upsert.redrivable
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not record the review")
        await mark_delivery(delivery_id, "error", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database unavailable; redeliver",
        ) from exc

    if not redrivable:
        await mark_delivery(delivery_id, "already_resolved", reason="review already finished")
        return {
            "status": "already_reviewed",
            "review_id": review_id,
            "delivery_id": delivery_id,
        }

    # 6. enqueue and return
    try:
        job_id = await enqueue_review(
            review_id=review_id,
            repo=event.repo,
            pr_number=event.pr_number,
            head_sha=event.head_sha,
            delivery_id=delivery_id,
            settings=context.settings,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("queue unavailable: %s", exc)
        await mark_delivery(delivery_id, "enqueue_failed", reason=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="review queue unavailable; redeliver",
        ) from exc

    logger.info("enqueued review", extra={"context": {"event": summarize(event), "job": job_id}})
    return {
        "status": "enqueued",
        "review_id": review_id,
        "job_id": job_id,
        "created": created,
        "delivery_id": delivery_id,
    }
