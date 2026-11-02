"""Circuit breakers, one per external service.

When a provider is down, retrying every call for the full timeout budget turns a
provider outage into our outage. Opening the circuit fails fast, and the review
degrades to a partial result rather than stalling the queue.

Process-local by design: correct for a single worker, and the honest limitation is
recorded in the README rather than hidden. A shared breaker needs a shared store,
which is a Redis round trip on every external call -- not worth it at this stage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    def __init__(self, name: str, retry_in: float) -> None:
        super().__init__(f"circuit '{name}' is open; retry in {retry_in:.1f}s")
        self.name = name
        self.retry_in = retry_in


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    reset_timeout: float = 30.0
    half_open_successes: int = 1

    state: BreakerState = BreakerState.CLOSED
    failures: int = 0
    successes_in_half_open: int = 0
    opened_at: float | None = None
    total_calls: int = 0
    total_failures: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        async with self._lock:
            self._maybe_half_open()
            if self.state is BreakerState.OPEN:
                assert self.opened_at is not None  # noqa: S101
                retry_in = max(0.0, self.reset_timeout - (time.monotonic() - self.opened_at))
                raise CircuitOpenError(self.name, retry_in)
            self.total_calls += 1

        try:
            result = await fn()
        except Exception:
            await self._on_failure()
            raise
        await self._on_success()
        return result

    def _maybe_half_open(self) -> None:
        if (
            self.state is BreakerState.OPEN
            and self.opened_at is not None
            and time.monotonic() - self.opened_at >= self.reset_timeout
        ):
            self.state = BreakerState.HALF_OPEN
            self.successes_in_half_open = 0
            logger.info("circuit '%s' half-open: probing", self.name)

    async def _on_failure(self) -> None:
        async with self._lock:
            self.failures += 1
            self.total_failures += 1
            if self.state is BreakerState.HALF_OPEN or self.failures >= self.failure_threshold:
                self.state = BreakerState.OPEN
                self.opened_at = time.monotonic()
                logger.warning(
                    "circuit '%s' opened after %d consecutive failures", self.name, self.failures
                )

    async def _on_success(self) -> None:
        async with self._lock:
            self.failures = 0
            if self.state is BreakerState.HALF_OPEN:
                self.successes_in_half_open += 1
                if self.successes_in_half_open >= self.half_open_successes:
                    self.state = BreakerState.CLOSED
                    self.opened_at = None
                    logger.info("circuit '%s' closed", self.name)

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failures": self.failures,
            "calls": self.total_calls,
            "failed_calls": self.total_failures,
        }


_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str, **kwargs: object) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name=name, **kwargs)  # type: ignore[arg-type]
    return _breakers[name]


def breaker_snapshots() -> list[dict[str, object]]:
    return [b.snapshot() for b in _breakers.values()]


def reset_breakers() -> None:
    """Tests only: breakers must not leak state between cases."""
    _breakers.clear()
