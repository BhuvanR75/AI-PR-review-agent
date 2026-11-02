"""Each reliability mechanic is mapped to a named failure mode, so each is tested
against the failure it exists for rather than for its own sake."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from backend.reliability.circuit_breaker import (
    BreakerState,
    CircuitOpenError,
    get_breaker,
    reset_breakers,
)
from backend.reliability.idempotency import mark_delivery, reserve_delivery
from backend.reliability.retry import RetryPolicy, call_with_retry
from backend.reliability.timeout import TimeoutExceeded, with_timeout


@pytest.fixture(autouse=True)
def _clean_breakers() -> None:
    reset_breakers()
    yield
    reset_breakers()


# --- retries (tool / API timeout) -------------------------------------------
async def test_retries_until_success() -> None:
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ConnectionError("provider hiccup")
        return "ok"

    result = await call_with_retry(
        flaky, policy=RetryPolicy(attempts=3, base_delay=0.001, label="test")
    )
    assert result == "ok"
    assert attempts["count"] == 3


async def test_reraises_after_exhausting_attempts() -> None:
    attempts = {"count": 0}

    async def always_fails() -> str:
        attempts["count"] += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await call_with_retry(
            always_fails, policy=RetryPolicy(attempts=2, base_delay=0.001)
        )
    assert attempts["count"] == 2


async def test_only_the_declared_exception_types_are_retried() -> None:
    attempts = {"count": 0}

    async def raises_value_error() -> str:
        attempts["count"] += 1
        raise ValueError("not retryable")

    with pytest.raises(ValueError):
        await call_with_retry(
            raises_value_error,
            policy=RetryPolicy(attempts=5, base_delay=0.001, retry_on=(ConnectionError,)),
        )
    assert attempts["count"] == 1


# --- timeouts (orchestration deadlock) --------------------------------------
async def test_timeout_raises_its_own_error_type() -> None:
    async def slow() -> str:
        await asyncio.sleep(5)
        return "never"

    with pytest.raises(TimeoutExceeded) as exc:
        await with_timeout(slow(), 0.01, label="security pass")
    assert exc.value.label == "security pass"
    assert exc.value.seconds == 0.01


async def test_fast_work_is_returned_untouched() -> None:
    async def quick() -> int:
        return 7

    assert await with_timeout(quick(), 5) == 7


# --- circuit breaker (a provider outage must not become ours) ---------------
async def test_breaker_opens_after_the_threshold() -> None:
    breaker = get_breaker("provider", failure_threshold=3, reset_timeout=60)

    async def boom() -> str:
        raise ConnectionError("down")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await breaker.call(boom)

    assert breaker.state is BreakerState.OPEN
    with pytest.raises(CircuitOpenError) as exc:
        await breaker.call(boom)
    assert exc.value.retry_in > 0


async def test_open_breaker_fails_fast_without_calling() -> None:
    breaker = get_breaker("provider", failure_threshold=1, reset_timeout=60)
    calls = {"count": 0}

    async def boom() -> str:
        calls["count"] += 1
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    with pytest.raises(CircuitOpenError):
        await breaker.call(boom)
    assert calls["count"] == 1


async def test_breaker_half_opens_then_closes_on_success() -> None:
    breaker = get_breaker("provider", failure_threshold=1, reset_timeout=0.01)

    async def boom() -> str:
        raise ConnectionError("down")

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    await asyncio.sleep(0.02)

    async def healthy() -> str:
        return "ok"

    assert await breaker.call(healthy) == "ok"
    assert breaker.state is BreakerState.CLOSED
    assert breaker.failures == 0


async def test_success_resets_the_failure_count() -> None:
    breaker = get_breaker("provider", failure_threshold=3, reset_timeout=60)

    async def boom() -> str:
        raise ConnectionError("down")

    async def fine() -> str:
        return "ok"

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    await breaker.call(fine)
    assert breaker.failures == 0

    with pytest.raises(ConnectionError):
        await breaker.call(boom)
    assert breaker.state is BreakerState.CLOSED


def test_breaker_snapshot_is_reportable() -> None:
    snapshot = get_breaker("provider", failure_threshold=2).snapshot()
    assert snapshot["state"] == "closed"
    assert snapshot["name"] == "provider"


# --- idempotency (a redelivered webhook) ------------------------------------
async def test_first_delivery_is_reserved(fake_pool: Any) -> None:
    fake_pool.fetchval_result = "delivery-1"
    reserved = await reserve_delivery(
        delivery_id="delivery-1",
        event_type="pull_request",
        action="opened",
        repo="acme/shop",
        pr_number=42,
        pool=fake_pool,
    )
    assert reserved is True
    assert "INSERT INTO WEBHOOK_DELIVERIES" in fake_pool.executed[0]
    assert "ON CONFLICT" in fake_pool.executed[0]


async def test_redelivery_is_refused(fake_pool: Any) -> None:
    """ON CONFLICT DO NOTHING returns no row, which is what makes the check
    correct under concurrent redelivery rather than merely usually correct."""
    fake_pool.fetchval_result = None
    reserved = await reserve_delivery(
        delivery_id="delivery-1", event_type="pull_request", pool=fake_pool
    )
    assert reserved is False


async def test_marking_a_delivery_records_status(fake_pool: Any) -> None:
    await mark_delivery("delivery-1", "done", pool=fake_pool)
    assert "UPDATE WEBHOOK_DELIVERIES" in fake_pool.executed[0]
