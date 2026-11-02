"""Timeouts. Every step gets one, which is what stops a single stalled agent from
hanging a join forever (the orchestration-deadlock failure mode)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class TimeoutExceeded(Exception):
    """Raised instead of asyncio.TimeoutError so callers can distinguish 'our step
    ran out of budget' from an unrelated timeout deeper in the stack."""

    def __init__(self, label: str, seconds: float) -> None:
        super().__init__(f"{label} exceeded {seconds:g}s")
        self.label = label
        self.seconds = seconds


async def with_timeout[T](
    awaitable: Awaitable[T],
    seconds: float,
    *,
    label: str = "operation",
) -> T:
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError as exc:
        logger.warning("%s timed out after %gs", label, seconds)
        raise TimeoutExceeded(label, seconds) from exc


async def call_with_timeout[T](
    fn: Callable[[], Awaitable[T]],
    seconds: float,
    *,
    label: str = "operation",
) -> T:
    return await with_timeout(fn(), seconds, label=label)
