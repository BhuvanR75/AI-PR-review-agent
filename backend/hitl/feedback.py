"""Feedback capture and the evidence threshold.

Feedback is recorded from day one and deliberately *not* acted on. Two or three
rejections from one reviewer are not a preference, and a system that reshapes itself
around them is the feedback-loop-poisoning failure mode with extra steps.

So this module collects the signal, counts it per agent, and refuses to make a
recommendation until there is enough of it. When there is, the recommendation is a
sentence for a human to read -- not an automatic threshold change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from backend.database.pg import get_pool

logger = logging.getLogger(__name__)

#: Rejections needed before a per-agent pattern may be called a pattern.
MIN_EVIDENCE_FOR_ACTION = 5

#: Rejection rate above which the agent's prompt or retrieval is probably wrong
#: rather than the model.
SUSPICIOUS_REJECTION_RATE = 0.4

_SIGNAL_SQL = """
SELECT f.agent_type                                          AS agent,
       count(*) FILTER (WHERE fb.kind = 'approved')          AS approvals,
       count(*) FILTER (WHERE fb.kind = 'rejected')          AS rejections,
       count(*) FILTER (WHERE fb.kind = 'edited')            AS edits
FROM hitl_feedback fb
JOIN finding_records f ON f.id = fb.finding_id
WHERE fb.created_at >= now() - make_interval(hours => $1)
GROUP BY f.agent_type
ORDER BY f.agent_type
"""


@dataclass
class FeedbackSignal:
    agent: str
    approvals: int = 0
    rejections: int = 0
    edits: int = 0

    @property
    def decided(self) -> int:
        return self.approvals + self.rejections

    @property
    def rejection_rate(self) -> float:
        return self.rejections / self.decided if self.decided else 0.0

    @property
    def enough_evidence(self) -> bool:
        return self.rejections >= MIN_EVIDENCE_FOR_ACTION

    @property
    def recommendation(self) -> str:
        if not self.enough_evidence:
            return (
                f"collecting: {self.rejections}/{MIN_EVIDENCE_FOR_ACTION} rejections "
                f"needed before any inference"
            )
        if self.rejection_rate > SUSPICIOUS_REJECTION_RATE:
            return (
                "investigate the prompt or the retrieved context for this pass -- "
                "the rejection rate suggests the findings are wrong rather than the model"
            )
        return "within tolerance; no change indicated"

    def as_dict(self) -> dict[str, object]:
        return {
            "agent": self.agent,
            "approvals": self.approvals,
            "rejections": self.rejections,
            "edits": self.edits,
            "rejection_rate": round(self.rejection_rate, 4),
            "enough_evidence": self.enough_evidence,
            "recommendation": self.recommendation,
        }


async def feedback_signals(
    hours: int = 24 * 7, *, pool: asyncpg.Pool | None = None
) -> list[FeedbackSignal]:
    target = pool if pool is not None else get_pool()
    rows = await target.fetch(_SIGNAL_SQL, hours)
    return [
        FeedbackSignal(
            agent=r["agent"],
            approvals=int(r["approvals"] or 0),
            rejections=int(r["rejections"] or 0),
            edits=int(r["edits"] or 0),
        )
        for r in rows
    ]
