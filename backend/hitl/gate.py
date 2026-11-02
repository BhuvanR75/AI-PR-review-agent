"""The confidence-weighted gate (L7).

Order of the checks is the design:

  1. Any CRITICAL finding escalates, regardless of confidence. Consequence of error
     dominates every other signal -- and note this is an explicit rule, not a
     comparison, because "severity is high enough" is a policy a human owns.
  2. `HITL_MODE=always` routes everything to a person. This is the default, and it is
     the study's own advice: start with more human involvement than you think you
     need, and remove a checkpoint once the system has earned it.
  3. Otherwise, post automatically only above the confidence threshold.

The system therefore ships at "human handles exceptions" with an escalation path to
"human decides", and cannot reach "full automation" without someone changing a
setting. That is not a limitation to be removed later; it is the intended posture
until approval outcomes justify more autonomy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.enums import Decision, Severity
from backend.models.findings import Finding
from backend.settings import Settings, get_settings


@dataclass(frozen=True)
class GateResult:
    decision: Decision
    reason: str
    hitl_mode: str
    threshold: float
    overall_confidence: float
    critical_findings: list[Finding] = field(default_factory=list)

    @property
    def requires_human(self) -> bool:
        return self.decision in {Decision.HUMAN_APPROVAL, Decision.ESCALATE}

    @property
    def escalates(self) -> bool:
        return self.decision is Decision.ESCALATE

    def as_event_payload(self) -> dict[str, object]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "hitl_mode": self.hitl_mode,
            "threshold": self.threshold,
            "overall_confidence": self.overall_confidence,
            "critical_findings": len(self.critical_findings),
        }


def apply_gate(
    findings: list[Finding],
    overall_confidence: float,
    settings: Settings | None = None,
) -> GateResult:
    resolved = settings or get_settings()
    critical = [f for f in findings if f.severity is Severity.CRITICAL]

    if critical:
        return GateResult(
            decision=Decision.ESCALATE,
            reason=(
                f"{len(critical)} CRITICAL finding(s) present: a missed critical issue is "
                f"too expensive to automate"
            ),
            hitl_mode=resolved.hitl_mode,
            threshold=resolved.hitl_confidence_threshold,
            overall_confidence=overall_confidence,
            critical_findings=critical,
        )

    if resolved.hitl_mode == "always":
        return GateResult(
            decision=Decision.HUMAN_APPROVAL,
            reason="HITL_MODE=always: every review waits for a human before posting",
            hitl_mode=resolved.hitl_mode,
            threshold=resolved.hitl_confidence_threshold,
            overall_confidence=overall_confidence,
        )

    if overall_confidence < resolved.hitl_confidence_threshold:
        return GateResult(
            decision=Decision.HUMAN_APPROVAL,
            reason=(
                f"overall confidence {overall_confidence:.2f} is below the "
                f"{resolved.hitl_confidence_threshold:.2f} threshold"
            ),
            hitl_mode=resolved.hitl_mode,
            threshold=resolved.hitl_confidence_threshold,
            overall_confidence=overall_confidence,
        )

    return GateResult(
        decision=Decision.POST,
        reason=(
            f"confidence {overall_confidence:.2f} at or above the "
            f"{resolved.hitl_confidence_threshold:.2f} threshold with no CRITICAL findings"
        ),
        hitl_mode=resolved.hitl_mode,
        threshold=resolved.hitl_confidence_threshold,
        overall_confidence=overall_confidence,
    )
