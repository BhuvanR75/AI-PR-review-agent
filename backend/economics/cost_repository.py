"""The cost ledger: three queries against the same spine the trace viewer reads.

When TimescaleDB is present the rollups are read; when it is not, the same answers
come from the raw hypertable (which is then an ordinary table) over a bounded time
window. The caller cannot tell the difference, and that is the point -- the
consolidation is behind a repository method, not spread through the dashboard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import asyncpg

from backend.models.review import ReviewCost
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_REVIEW_COST_SQL = """
SELECT coalesce(sum(cost_usd), 0)                                        AS usd,
       coalesce(sum(tokens_in), 0)                                       AS tokens_in,
       coalesce(sum(tokens_out), 0)                                      AS tokens_out,
       count(*) FILTER (WHERE event_type = 'llm.call')                   AS llm_calls
FROM agent_events
WHERE review_id = $1
"""

_REVIEW_COST_BY_AGENT_SQL = """
SELECT agent, coalesce(sum(cost_usd), 0) AS usd
FROM agent_events
WHERE review_id = $1
GROUP BY agent
"""

_DAILY_COST_SQL = """
SELECT coalesce(sum(cost_usd), 0) AS usd
FROM agent_events
WHERE ts >= date_trunc('day', now())
"""

_AGENT_HEALTH_RAW_SQL = """
SELECT agent,
       count(*) FILTER (WHERE event_type = 'llm.call')      AS llm_calls,
       coalesce(sum(cost_usd), 0)                           AS cost_usd,
       coalesce(sum(tokens_in), 0)                          AS tokens_in,
       coalesce(sum(tokens_out), 0)                         AS tokens_out,
       avg(latency_ms)                                      AS avg_latency_ms,
       max(latency_ms)                                      AS max_latency_ms,
       count(*) FILTER (WHERE outcome = 'rejected')         AS rejected,
       count(*) FILTER (WHERE outcome = 'escalated')        AS escalated,
       count(*) FILTER (WHERE outcome IS NOT NULL)          AS decisions
FROM agent_events
WHERE ts >= now() - make_interval(hours => $1)
GROUP BY agent
ORDER BY agent
"""

_AGENT_HEALTH_ROLLUP_SQL = """
SELECT agent,
       sum(llm_calls)      AS llm_calls,
       sum(cost_usd)       AS cost_usd,
       sum(tokens_in)      AS tokens_in,
       sum(tokens_out)     AS tokens_out,
       avg(avg_latency_ms) AS avg_latency_ms,
       max(max_latency_ms) AS max_latency_ms,
       sum(rejected)       AS rejected,
       sum(escalated)      AS escalated,
       sum(decisions)      AS decisions
FROM agent_health_1m
WHERE bucket >= now() - make_interval(hours => $1)
GROUP BY agent
ORDER BY agent
"""

_P95_SQL = """
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95
FROM agent_events
WHERE agent = $1
  AND event_type = 'llm.call'
  AND latency_ms IS NOT NULL
  AND ts >= now() - make_interval(hours => $2)
"""

_PR_COST_SQL = """
SELECT review_id::text AS review_id,
       coalesce(sum(cost_usd), 0) AS cost_usd,
       count(*) FILTER (WHERE event_type = 'llm.call') AS llm_calls
FROM agent_events
WHERE review_id IS NOT NULL
  AND ts >= now() - make_interval(hours => $1)
GROUP BY review_id
ORDER BY cost_usd DESC
LIMIT $2
"""


@dataclass
class AgentHealth:
    agent: str
    llm_calls: int = 0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    avg_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    rejected: int = 0
    escalated: int = 0
    decisions: int = 0
    p95_latency_ms: float | None = None
    source: str = "raw"

    @property
    def rejection_rate(self) -> float:
        """Rising rejection rate per agent is the drift signal, not a vanity metric."""
        return self.rejected / self.decisions if self.decisions else 0.0

    @property
    def escalation_rate(self) -> float:
        return self.escalated / self.decisions if self.decisions else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "llm_calls": self.llm_calls,
            "cost_usd": round(self.cost_usd, 6),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "max_latency_ms": self.max_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "rejected": self.rejected,
            "escalated": self.escalated,
            "decisions": self.decisions,
            "rejection_rate": round(self.rejection_rate, 4),
            "escalation_rate": round(self.escalation_rate, 4),
            "source": self.source,
        }


@dataclass
class EconomicsSnapshot:
    daily_cost_usd: float = 0.0
    budget_usd: float = 0.0
    remaining_usd: float = 0.0
    by_agent: list[AgentHealth] = field(default_factory=list)

    @property
    def utilisation(self) -> float:
        return self.daily_cost_usd / self.budget_usd if self.budget_usd else 0.0


class CostRepository:
    def __init__(self, pool: asyncpg.Pool, settings: Settings | None = None) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.use_rollups = False

    async def detect_capabilities(self) -> None:
        """Prefer the rollups when the hypertable actually exists; otherwise the raw
        table answers the same questions."""
        try:
            self.use_rollups = bool(
                await self.pool.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates"
                    " WHERE view_name = 'agent_health_1m')"
                )
            )
        except Exception:  # noqa: BLE001 - Timescale absent
            self.use_rollups = False

    async def review_cost(self, review_id: str) -> ReviewCost:
        row = await self.pool.fetchrow(_REVIEW_COST_SQL, _uuid(review_id))
        by_agent_rows = await self.pool.fetch(_REVIEW_COST_BY_AGENT_SQL, _uuid(review_id))
        return ReviewCost(
            usd=float(row["usd"] or 0.0) if row else 0.0,
            tokens_in=int(row["tokens_in"] or 0) if row else 0,
            tokens_out=int(row["tokens_out"] or 0) if row else 0,
            llm_calls=int(row["llm_calls"] or 0) if row else 0,
            by_agent={r["agent"]: round(float(r["usd"] or 0.0), 6) for r in by_agent_rows},
        )

    async def costs_for_reviews(self, review_ids: list[str]) -> dict[str, ReviewCost]:
        if not review_ids:
            return {}
        rows = await self.pool.fetch(
            """
            SELECT review_id::text AS review_id,
                   coalesce(sum(cost_usd), 0)                  AS usd,
                   coalesce(sum(tokens_in), 0)                 AS tokens_in,
                   coalesce(sum(tokens_out), 0)                AS tokens_out,
                   count(*) FILTER (WHERE event_type = 'llm.call') AS llm_calls
            FROM agent_events
            WHERE review_id = ANY($1::uuid[])
            GROUP BY review_id
            """,
            [_uuid(r) for r in review_ids],
        )
        return {
            r["review_id"]: ReviewCost(
                usd=float(r["usd"] or 0.0),
                tokens_in=int(r["tokens_in"] or 0),
                tokens_out=int(r["tokens_out"] or 0),
                llm_calls=int(r["llm_calls"] or 0),
            )
            for r in rows
        }

    async def daily_cost(self) -> float:
        value = await self.pool.fetchval(_DAILY_COST_SQL)
        return float(value or 0.0)

    async def agent_health(self, hours: int = 6, *, with_p95: bool = False) -> list[AgentHealth]:
        source = "rollup" if self.use_rollups else "raw"
        sql = _AGENT_HEALTH_ROLLUP_SQL if self.use_rollups else _AGENT_HEALTH_RAW_SQL
        try:
            rows = await self.pool.fetch(sql, hours)
        except Exception as exc:  # noqa: BLE001 - rollup vanished mid-flight
            logger.warning("rollup read failed (%s), falling back to raw events", exc)
            rows = await self.pool.fetch(_AGENT_HEALTH_RAW_SQL, hours)
            source = "raw"

        health = [
            AgentHealth(
                agent=r["agent"],
                llm_calls=int(r["llm_calls"] or 0),
                cost_usd=float(r["cost_usd"] or 0.0),
                tokens_in=int(r["tokens_in"] or 0),
                tokens_out=int(r["tokens_out"] or 0),
                avg_latency_ms=float(r["avg_latency_ms"] or 0.0),
                max_latency_ms=float(r["max_latency_ms"] or 0.0),
                rejected=int(r["rejected"] or 0),
                escalated=int(r["escalated"] or 0),
                decisions=int(r["decisions"] or 0),
                source=source,
            )
            for r in rows
        ]
        if with_p95:
            for entry in health:
                entry.p95_latency_ms = await self.p95_latency(entry.agent, hours=hours)
        return health

    async def p95_latency(self, agent: str, hours: int = 1) -> float | None:
        """Computed exactly from the raw spine over a bounded window rather than
        shipped in the rollup, because continuous aggregates cannot use ordered-set
        aggregates and the toolkit's percentile_agg is not guaranteed present."""
        value = await self.pool.fetchval(_P95_SQL, agent, hours)
        return float(value) if value is not None else None

    async def recent_pr_costs(self, hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
        rows = await self.pool.fetch(_PR_COST_SQL, hours, limit)
        return [
            {
                "review_id": r["review_id"],
                "cost_usd": round(float(r["cost_usd"] or 0.0), 6),
                "llm_calls": int(r["llm_calls"] or 0),
            }
            for r in rows
        ]

    async def snapshot(self, hours: int = 24) -> EconomicsSnapshot:
        daily = await self.daily_cost()
        return EconomicsSnapshot(
            daily_cost_usd=round(daily, 6),
            budget_usd=self.settings.daily_budget_usd,
            remaining_usd=round(max(0.0, self.settings.daily_budget_usd - daily), 6),
            by_agent=await self.agent_health(hours=hours, with_p95=True),
        )


def _uuid(value: str) -> Any:
    import uuid as _u

    try:
        return _u.UUID(str(value))
    except ValueError:
        return None
