"""Routing advice, derived from observed cost rather than from intuition.

Phase 16 of the study asks for token-cost attribution and routing efficiency. The
honest form of that at this stage is a report: which agent costs what, and which
agents are cheap enough to move to a smaller model. It does not silently re-route --
changing the model that reviews security code is a decision a human should make
deliberately, not something a heuristic does at 3am.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.economics.cost_repository import AgentHealth
from backend.settings import Settings, get_settings

#: Agents where a cheaper model is a reasonable experiment. Security is absent on
#: purpose: a miss there is the expensive kind.
CHEAPENABLE_AGENTS = frozenset({"docs", "tests"})


@dataclass
class RoutingAdvice:
    agent: str
    current_cost_usd: float
    share_of_spend: float
    recommendation: str
    rationale: str


def advise(health: list[AgentHealth], settings: Settings | None = None) -> list[RoutingAdvice]:
    settings = settings or get_settings()
    total = sum(h.cost_usd for h in health) or 0.0
    advice: list[RoutingAdvice] = []

    for entry in sorted(health, key=lambda h: h.cost_usd, reverse=True):
        share = entry.cost_usd / total if total else 0.0
        if entry.agent in CHEAPENABLE_AGENTS and share > 0.15:
            recommendation = "consider a smaller model"
            rationale = (
                f"accounts for {share:.0%} of LLM spend; the failure cost of a missed "
                f"{entry.agent} finding is lower than in security or quality"
            )
        elif entry.rejection_rate > 0.4 and entry.decisions >= 5:
            recommendation = "inspect prompts before spending more"
            rationale = (
                f"{entry.rejection_rate:.0%} of this agent's findings were rejected by a "
                f"human across {entry.decisions} decisions -- prompt or retrieval problem, "
                f"not a model problem"
            )
        elif entry.cost_usd == 0.0 and entry.llm_calls == 0:
            recommendation = "no traffic"
            rationale = "no LLM calls recorded in the window"
        else:
            recommendation = "leave as is"
            rationale = f"{share:.0%} of spend, rejection rate {entry.rejection_rate:.0%}"

        advice.append(
            RoutingAdvice(
                agent=entry.agent,
                current_cost_usd=round(entry.cost_usd, 6),
                share_of_spend=round(share, 4),
                recommendation=recommendation,
                rationale=rationale,
            )
        )

    if total and settings.daily_budget_usd:
        utilisation = total / settings.daily_budget_usd
        if utilisation > 0.8:
            advice.append(
                RoutingAdvice(
                    agent="all",
                    current_cost_usd=round(total, 6),
                    share_of_spend=1.0,
                    recommendation="approaching the daily cap",
                    rationale=(
                        f"projected spend is {utilisation:.0%} of the daily budget; "
                        f"BudgetGuard will hard-block further LLM calls at the limit"
                    ),
                )
            )
    return advice
