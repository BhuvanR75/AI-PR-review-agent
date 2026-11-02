"""The gate is the system's autonomy policy, so every branch is asserted explicitly --
including the fact that the shipped default cannot post anything on its own."""

from __future__ import annotations

from backend.hitl.gate import apply_gate
from backend.models.enums import AgentType, Category, Decision, Severity
from backend.models.findings import Finding
from backend.settings import Settings


def finding(severity: Severity, confidence: float = 0.9) -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=severity,
        category=Category.INJECTION,
        summary="something",
        file_path="app.py",
        line_start=1,
        confidence=confidence,
        rationale="because",
    )


def settings(**overrides: object) -> Settings:
    base = Settings(hitl_mode="always", hitl_confidence_threshold=0.75)
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def test_critical_escalates_under_always_mode() -> None:
    result = apply_gate([finding(Severity.CRITICAL)], 0.99, settings())
    assert result.decision is Decision.ESCALATE
    assert result.requires_human
    assert result.escalates
    assert len(result.critical_findings) == 1
    assert "CRITICAL" in result.reason


def test_critical_escalates_even_in_confidence_gated_mode() -> None:
    """Consequence of error dominates: perfect confidence must not unlock a critical."""
    result = apply_gate(
        [finding(Severity.CRITICAL)], 1.0, settings(hitl_mode="confidence_gated")
    )
    assert result.decision is Decision.ESCALATE


def test_default_mode_sends_everything_to_a_human() -> None:
    result = apply_gate([finding(Severity.LOW)], 1.0, settings())
    assert result.decision is Decision.HUMAN_APPROVAL
    assert result.requires_human
    assert not result.escalates
    assert "always" in result.reason


def test_default_mode_routes_a_clean_review_to_a_human_too() -> None:
    """No findings is not the same as safe, so the default still asks."""
    assert apply_gate([], 1.0, settings()).decision is Decision.HUMAN_APPROVAL


def test_confidence_gated_mode_posts_above_the_threshold() -> None:
    result = apply_gate(
        [finding(Severity.MEDIUM)], 0.9, settings(hitl_mode="confidence_gated")
    )
    assert result.decision is Decision.POST
    assert not result.requires_human


def test_confidence_gated_mode_defers_below_the_threshold() -> None:
    result = apply_gate(
        [finding(Severity.MEDIUM)], 0.5, settings(hitl_mode="confidence_gated")
    )
    assert result.decision is Decision.HUMAN_APPROVAL
    assert "below" in result.reason


def test_threshold_is_inclusive() -> None:
    result = apply_gate(
        [finding(Severity.MEDIUM)], 0.75, settings(hitl_mode="confidence_gated")
    )
    assert result.decision is Decision.POST


def test_high_severity_without_critical_can_still_auto_post() -> None:
    """HIGH is not CRITICAL: the escalation rule is about consequence, not vibes."""
    result = apply_gate(
        [finding(Severity.HIGH)], 0.99, settings(hitl_mode="confidence_gated")
    )
    assert result.decision is Decision.POST


def test_event_payload_is_recordable() -> None:
    payload = apply_gate([finding(Severity.CRITICAL)], 0.8, settings()).as_event_payload()
    assert payload["decision"] == "escalate"
    assert payload["critical_findings"] == 1
    assert payload["hitl_mode"] == "always"
