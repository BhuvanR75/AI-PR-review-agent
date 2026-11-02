"""BudgetGuard.

The daily cap is checked at the top of every agent run, before any LLM call, and it
hard-blocks rather than warning. A cost comment in a log is not a control; refusing
to spend is.

Blocking degrades to slower-but-correct in the way that matters here: the review
still completes, marked incomplete, with no findings fabricated to fill the gap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.economics.cost_repository import CostRepository
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class BudgetExceeded(RuntimeError):
    def __init__(self, spent: float, limit: float) -> None:
        super().__init__(f"daily LLM budget exceeded: spent ${spent:.4f} of ${limit:.2f}")
        self.spent = spent
        self.limit = limit


@dataclass(frozen=True)
class BudgetStatus:
    allowed: bool
    spent_today: float
    limit: float
    remaining: float
    reason: str

    @property
    def utilisation(self) -> float:
        return self.spent_today / self.limit if self.limit else 0.0


class BudgetGuard:
    def __init__(self, cost_repository: CostRepository, settings: Settings | None = None) -> None:
        self.costs = cost_repository
        self.settings = settings or get_settings()

    async def status(self) -> BudgetStatus:
        limit = self.settings.daily_budget_usd
        if limit <= 0:
            return BudgetStatus(
                allowed=True,
                spent_today=await self.costs.daily_cost(),
                limit=0.0,
                remaining=float("inf"),
                reason="budget disabled",
            )
        spent = await self.costs.daily_cost()
        remaining = round(max(0.0, limit - spent), 6)
        allowed = spent < limit
        return BudgetStatus(
            allowed=allowed,
            spent_today=round(spent, 6),
            limit=limit,
            remaining=remaining,
            reason="within budget" if allowed else f"spent ${spent:.4f} of ${limit:.2f}",
        )

    async def ensure(self) -> BudgetStatus:
        status = await self.status()
        if not status.allowed:
            logger.error(
                "blocking LLM work: %s",
                status.reason,
                extra={"context": {"spent": status.spent_today, "limit": status.limit}},
            )
            raise BudgetExceeded(status.spent_today, status.limit)
        return status
