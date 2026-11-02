"""Economics and monitoring endpoints.

Everything here reads the same events spine the trace viewer reads. Nothing is
double-counted in a side table, which is the practical payoff of the one-spine
decision: "what did today cost" and "what did this review actually do" cannot disagree.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from backend.api.deps import get_app_context
from backend.database.pg import get_pool
from backend.economics.cost_repository import CostRepository
from backend.economics.routing_advisor import advise
from backend.hitl.feedback import feedback_signals
from backend.observability.alerting import collect_alerts

router = APIRouter(prefix="/api/economics", tags=["economics"])


@router.get("/summary")
async def summary(request: Request, hours: int = Query(default=24, ge=1, le=720)) -> dict:
    context = get_app_context(request)
    costs = CostRepository(get_pool(), context.settings)
    snapshot = await costs.snapshot(hours=hours)
    return {
        "daily_cost_usd": snapshot.daily_cost_usd,
        "budget_usd": snapshot.budget_usd,
        "remaining_usd": snapshot.remaining_usd,
        "utilisation": round(snapshot.utilisation, 4),
        "by_agent": [h.as_dict() for h in snapshot.by_agent],
        "recent_pr_costs": await costs.recent_pr_costs(hours=hours),
        "rollups_available": costs.use_rollups,
    }


@router.get("/feedback")
async def feedback(request: Request, hours: int = Query(default=168, ge=1, le=8760)) -> dict:
    signals = await feedback_signals(hours, pool=get_pool())
    return {
        "window_hours": hours,
        "signals": [s.as_dict() for s in signals],
        "note": (
            "rejections are counted, not acted on: a minimum evidence threshold must be "
            "met before any inference about agent quality"
        ),
    }


@router.get("/advice")
async def advice(request: Request, hours: int = Query(default=24, ge=1, le=720)) -> dict:
    context = get_app_context(request)
    health = await CostRepository(get_pool(), context.settings).agent_health(hours=hours)
    return {
        "advice": [
            {
                "agent": a.agent,
                "cost_usd": a.current_cost_usd,
                "share_of_spend": a.share_of_spend,
                "recommendation": a.recommendation,
                "rationale": a.rationale,
            }
            for a in advise(health, context.settings)
        ]
    }


@router.get("/alerts")
async def alerts(request: Request) -> dict:
    context = get_app_context(request)
    collected = await collect_alerts(context.settings)
    return {"count": len(collected), "alerts": [a.as_dict() for a in collected]}
