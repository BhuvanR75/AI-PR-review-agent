"""Alert rules over the events spine.

Two of the study's failure modes surface here, and both are about *rates*, not
failures -- nothing is broken when they fire, which is exactly why they need an
explicit rule rather than an exception:

  * human bottleneck -- the approval queue is growing faster than people clear it.
  * model drift, via a rising rejection rate per agent.

Alert rules that are never checked are decoration, so these run on a worker cron.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from backend.database.pg import get_pool
from backend.economics.cost_repository import AgentHealth, CostRepository
from backend.hitl.feedback import FeedbackSignal, feedback_signals
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class AlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class Alert:
    kind: str
    severity: AlertSeverity
    message: str
    context: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "severity": self.severity.value,
            "message": self.message,
            "context": self.context,
        }


#: Queue age beyond which a human is the bottleneck rather than the queue being busy.
STALE_QUEUE_SECONDS = 60 * 60 * 4

#: Pending reviews that justify a capacity conversation.
QUEUE_DEPTH_WARNING = 25


async def collect_alerts(settings: Settings | None = None) -> list[Alert]:
    resolved = settings or get_settings()
    alerts: list[Alert] = []
    pool = get_pool()
    costs = CostRepository(pool, resolved)

    # --- cost ceiling -------------------------------------------------------
    spent = await costs.daily_cost()
    if resolved.daily_budget_usd > 0:
        utilisation = spent / resolved.daily_budget_usd
        if utilisation >= 1.0:
            alerts.append(
                Alert(
                    kind="budget_exhausted",
                    severity=AlertSeverity.CRITICAL,
                    message="daily LLM budget exhausted; further passes will be blocked",
                    context={"spent_usd": round(spent, 4), "limit_usd": resolved.daily_budget_usd},
                )
            )
        elif utilisation >= 0.8:
            alerts.append(
                Alert(
                    kind="budget_threshold",
                    severity=AlertSeverity.WARNING,
                    message=f"{utilisation:.0%} of the daily LLM budget spent",
                    context={"spent_usd": round(spent, 4), "limit_usd": resolved.daily_budget_usd},
                )
            )

    # --- the human bottleneck ----------------------------------------------
    from backend.hitl.queue import queue_depth

    depth = await queue_depth()
    if (
        depth.oldest_pending_seconds is not None
        and depth.oldest_pending_seconds > STALE_QUEUE_SECONDS
    ):
        hours = depth.oldest_pending_seconds / 3600
        alerts.append(
            Alert(
                kind="stale_approval_queue",
                severity=AlertSeverity.WARNING,
                message=f"oldest pending review has waited {hours:.1f}h",
                context={
                    "pending": depth.pending_approval,
                    "oldest_seconds": round(depth.oldest_pending_seconds),
                },
            )
        )
    if depth.pending_approval >= QUEUE_DEPTH_WARNING:
        alerts.append(
            Alert(
                kind="approval_queue_depth",
                severity=AlertSeverity.WARNING,
                message=f"{depth.pending_approval} reviews awaiting approval",
                context={"pending": depth.pending_approval},
            )
        )

    # --- drift, from rejections --------------------------------------------
    for signal in await feedback_signals(pool=pool):
        drift_alert = _drift_alert(signal)
        if drift_alert is not None:
            alerts.append(drift_alert)

    # --- degradation --------------------------------------------------------
    for health in await costs.agent_health(hours=6):
        alerts.extend(_health_alerts(health))

    # --- audit integrity ---------------------------------------------------
    from backend.observability.events import get_emit_failures

    if get_emit_failures() > 0:
        alerts.append(
            Alert(
                kind="events_spine_failures",
                severity=AlertSeverity.CRITICAL,
                message=(f"{get_emit_failures()} event write(s) failed; the audit trail has gaps"),
                context={"failures": get_emit_failures()},
            )
        )

    return alerts


def _drift_alert(signal: FeedbackSignal) -> Alert | None:
    if not signal.enough_evidence:
        return None
    if signal.rejection_rate <= 0.4:
        return None
    return Alert(
        kind="agent_rejection_drift",
        severity=AlertSeverity.WARNING,
        message=(
            f"the {signal.agent} pass has a {signal.rejection_rate:.0%} rejection rate "
            f"over {signal.decided} decisions"
        ),
        context=signal.as_dict(),
    )


def _health_alerts(health: AgentHealth) -> list[Alert]:
    alerts: list[Alert] = []
    if health.decisions and health.escalation_rate > 0.5:
        alerts.append(
            Alert(
                kind="escalation_rate",
                severity=AlertSeverity.WARNING,
                message=(
                    f"the {health.agent} pass escalates "
                    f"{health.escalation_rate:.0%} of reviews"
                ),
                context=health.as_dict(),
            )
        )
    return alerts
