"""Ingress idempotency.

GitHub retries deliveries. A retried `pull_request` webhook must be acknowledged and
dropped, never reviewed twice. The reservation is a single conditional INSERT, which
is what makes it correct under concurrent redelivery rather than merely usually
correct: two workers racing on the same delivery id cannot both win.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from backend.database.pg import get_pool

logger = logging.getLogger(__name__)


async def reserve_delivery(
    *,
    delivery_id: str,
    event_type: str,
    action: str | None = None,
    repo: str | None = None,
    pr_number: int | None = None,
    pool: asyncpg.Pool | None = None,
) -> bool:
    """Claim a delivery id. Returns True if this caller owns the work.

    False means the delivery was already seen -- the caller should acknowledge and
    do nothing further.
    """
    target = pool if pool is not None else get_pool()
    row = await target.fetchval(
        """
        INSERT INTO webhook_deliveries (delivery_id, event_type, action, repo, pr_number)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (delivery_id) DO NOTHING
        RETURNING delivery_id
        """,
        delivery_id,
        event_type,
        action,
        repo,
        pr_number,
    )
    if row is None:
        logger.info("duplicate delivery ignored", extra={"context": {"delivery_id": delivery_id}})
        return False
    return True


async def mark_delivery(
    delivery_id: str,
    status: str,
    *,
    reason: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> None:
    target = pool if pool is not None else get_pool()
    await target.execute(
        "UPDATE webhook_deliveries SET status = $2, reason = $3 WHERE delivery_id = $1",
        delivery_id,
        status,
        reason,
    )


async def get_delivery(
    delivery_id: str, *, pool: asyncpg.Pool | None = None
) -> dict[str, Any] | None:
    target = pool if pool is not None else get_pool()
    row = await target.fetchrow(
        "SELECT * FROM webhook_deliveries WHERE delivery_id = $1", delivery_id
    )
    return dict(row) if row else None
