"""The events spine (L6). One append-only, time-ordered table that three consumers
read: the trace viewer, the audit trail, and the cost ledger.

Two decisions worth stating plainly.

1. Every write is best-effort. If the spine is unreachable the review still runs,
   because refusing to do the work is a worse failure than losing a log line about
   it. The failure is counted (`emit_failures`) and logged at ERROR so it is
   visible rather than silent -- a spike here is an incident, not background noise.

2. There is no UPDATE or DELETE path in this module, by construction. Immutability
   is what makes the same table trustworthy as both a debugging trace and an audit
   record.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import asyncpg

from backend.database.pg import get_pool
from backend.models.enums import EventType, Outcome
from backend.models.review import AgentEvent

logger = logging.getLogger(__name__)

#: Incremented whenever a spine write fails. Surfaced by /healthz so a silent
#: audit outage is impossible to miss. Read it through get_emit_failures():
#: importing a module-level int by value would freeze it at import time.
emit_failures: int = 0


def get_emit_failures() -> int:
    return emit_failures


_INSERT_SQL = """
INSERT INTO agent_events (
    ts, review_id, agent, span_id, parent_span, event_type, model,
    tokens_in, tokens_out, cost_usd, latency_ms, outcome, confidence, payload
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
)
"""


def _as_uuid(value: str | uuid.UUID | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def emit_agent_event(
    *,
    agent: str,
    event_type: EventType | str,
    review_id: str | uuid.UUID | None = None,
    span_id: str | uuid.UUID | None = None,
    parent_span: str | uuid.UUID | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    cost_usd: float | None = None,
    latency_ms: int | None = None,
    outcome: str | None = None,
    confidence: float | None = None,
    payload: dict[str, Any] | None = None,
    pool: asyncpg.Pool | None = None,
    ts: datetime | None = None,
) -> None:
    """Append one row to the spine. Never raises."""
    global emit_failures
    try:
        target = pool if pool is not None else get_pool()
        await target.execute(
            _INSERT_SQL,
            ts or datetime.now(UTC),
            _as_uuid(review_id),
            str(agent),
            _as_uuid(span_id) or uuid.uuid4(),
            _as_uuid(parent_span),
            str(event_type),
            model,
            tokens_in,
            tokens_out,
            cost_usd,
            latency_ms,
            outcome,
            confidence,
            payload or {},
        )
    except Exception as exc:  # noqa: BLE001 - see module docstring, point 1
        emit_failures += 1
        logger.error("failed to write event %s for agent %s: %s", event_type, agent, exc)


@dataclass
class Span:
    """A unit of work with a start and an end, and a home for the events inside it.

    async with Span(agent="security", review_id=rid, note="grounded pass") as span:
        await span.event(EventType.LLM_CALL, model=..., cost_usd=..., latency_ms=...)
        span.outcome = Outcome.OK.value
    """

    agent: str
    review_id: str | None = None
    parent_span: str | None = None
    note: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    outcome: str | None = None
    confidence: float | None = None
    _started: float = field(default_factory=time.perf_counter, repr=False)
    _pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> Span:
        await self._emit(
            EventType.SPAN_START,
            payload={**self.payload, **({"note": self.note} if self.note else {})},
        )
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> Literal[False]:
        # `Literal[False]` rather than `bool`: this context manager never suppresses an
        # exception, and saying so precisely is what lets a type checker see that code
        # inside a `async with Span(...)` block really does run to completion.
        if exc_type is not None and self.outcome is None:
            self.outcome = Outcome.ERROR.value
        await self._emit(
            EventType.SPAN_END,
            latency_ms=self.elapsed_ms,
            outcome=self.outcome,
            confidence=self.confidence,
        )
        return False  # never swallow the exception

    @property
    def elapsed_ms(self) -> int:
        return int((time.perf_counter() - self._started) * 1000)

    async def event(
        self,
        event_type: EventType | str,
        *,
        model: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_usd: float | None = None,
        latency_ms: int | None = None,
        outcome: str | None = None,
        confidence: float | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._emit(
            event_type,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            outcome=outcome,
            confidence=confidence,
            payload=payload,
        )

    async def _emit(self, event_type: EventType | str, **kwargs: Any) -> None:
        await emit_agent_event(
            agent=self.agent,
            event_type=event_type,
            review_id=self.review_id,
            span_id=self.span_id,
            parent_span=self.parent_span,
            pool=self._pool,
            **kwargs,
        )


@asynccontextmanager
async def span(
    agent: str,
    review_id: str | None = None,
    *,
    parent_span: str | None = None,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AsyncIterator[Span]:
    async with Span(
        agent=agent,
        review_id=review_id,
        parent_span=parent_span,
        note=note,
        payload=payload or {},
    ) as s:
        yield s


# --- reads (the same table, three queries) -----------------------------------
_TRACE_SQL = """
SELECT ts, review_id, agent, span_id, parent_span, event_type, model,
       tokens_in, tokens_out, cost_usd, latency_ms, outcome, confidence, payload
FROM agent_events
WHERE review_id = $1
ORDER BY ts, id
LIMIT $2
"""


def _row_to_event(row: asyncpg.Record) -> AgentEvent:
    return AgentEvent(
        ts=row["ts"],
        review_id=str(row["review_id"]) if row["review_id"] else None,
        agent=row["agent"],
        span_id=str(row["span_id"]),
        parent_span=str(row["parent_span"]) if row["parent_span"] else None,
        event_type=row["event_type"],
        model=row["model"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        cost_usd=float(row["cost_usd"]) if row["cost_usd"] is not None else None,
        latency_ms=row["latency_ms"],
        outcome=row["outcome"],
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        payload=row["payload"],
    )


async def read_trace(
    review_id: str, *, limit: int = 500, pool: asyncpg.Pool | None = None
) -> list[AgentEvent]:
    """Reconstruct any review end to end. One indexed query on the spine."""
    target = pool if pool is not None else get_pool()
    rows = await target.fetch(_TRACE_SQL, _as_uuid(review_id), limit)
    return [_row_to_event(r) for r in rows]


async def count_events(review_id: str, *, pool: asyncpg.Pool | None = None) -> int:
    target = pool if pool is not None else get_pool()
    value = await target.fetchval(
        "SELECT count(*) FROM agent_events WHERE review_id = $1", _as_uuid(review_id)
    )
    return int(value or 0)
