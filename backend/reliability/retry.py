"""Retries with exponential backoff and jitter.

Jitter matters more than it looks: four specialists failing in lockstep against the
same provider and retrying on the same schedule is a self-inflicted thundering herd.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_exponential_jitter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.4
    max_delay: float = 8.0
    retry_on: tuple[type[BaseException], ...] = (Exception,)
    label: str = "call"


def build_retrying(policy: RetryPolicy) -> AsyncRetrying:
    return AsyncRetrying(
        stop=stop_after_attempt(max(1, policy.attempts)),
        wait=wait_exponential_jitter(initial=policy.base_delay, max=policy.max_delay),
        retry=retry_if_exception_type(policy.retry_on),
        reraise=True,
    )


async def call_with_retry[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
) -> T:
    """Run `fn`, retrying on the policy's exception types. Re-raises the final error
    so the caller decides what a failure means -- degrade, escalate, or stop."""
    resolved = policy or RetryPolicy()
    attempts = 0
    async for attempt in build_retrying(resolved):
        with attempt:
            attempts += 1
            if attempts > 1:
                logger.info(
                    "retrying %s (attempt %d/%d)", resolved.label, attempts, resolved.attempts
                )
            return await fn()
    raise RuntimeError("unreachable: tenacity with reraise=True always returns or raises")
