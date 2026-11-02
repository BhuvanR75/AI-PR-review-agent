"""Reliability mechanics (L8), each mapped to a named failure mode rather than
added speculatively:

  retries         -> tool / API timeout
  timeouts        -> orchestration deadlock
  circuit breaker -> provider outage becoming our outage
  idempotency     -> a retried webhook posting the same review twice
"""

from backend.reliability.circuit_breaker import (
    BreakerState,
    CircuitBreaker,
    CircuitOpenError,
    breaker_snapshots,
    get_breaker,
    reset_breakers,
)
from backend.reliability.idempotency import get_delivery, mark_delivery, reserve_delivery
from backend.reliability.retry import RetryPolicy, call_with_retry
from backend.reliability.timeout import TimeoutExceeded, with_timeout

__all__ = [
    "BreakerState",
    "CircuitBreaker",
    "CircuitOpenError",
    "RetryPolicy",
    "TimeoutExceeded",
    "breaker_snapshots",
    "call_with_retry",
    "get_breaker",
    "get_delivery",
    "mark_delivery",
    "reset_breakers",
    "reserve_delivery",
    "with_timeout",
]
