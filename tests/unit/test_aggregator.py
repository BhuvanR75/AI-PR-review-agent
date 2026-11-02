"""Aggregation is deterministic and routes work to humans, so it is tested as policy
rather than as code. The interesting cases are the disagreements between agents."""

from __future__ import annotations

from backend.models.enums import AgentType, Category, Severity
from backend.models.findings import Finding
from backend.orchestrator.aggregator import (
    merge_findings,
    needs_attention,
    overall_confidence,
    select_postable,
    severity_counts,
)


def finding(
    agent: AgentType,
    *,
    line: int | None = 10,
    severity: Severity = Severity.MEDIUM,
    confidence: float = 0.5,
    category: Category = Category.CORRECTNESS,
    path: str = "app.py",
) -> Finding:
    return Finding(
        agent_type=agent,
        severity=severity,
        category=category,
        summary=f"{agent.value} finding",
        file_path=path,
        line_start=line,
        confidence=confidence,
        rationale="because the code does X",
    )


def test_single_finding_passes_through() -> None:
    merged = merge_findings([finding(AgentType.QUALITY)])
    assert len(merged.findings) == 1
    assert merged.duplicates_collapsed == 0


def test_same_location_from_two_agents_collapses() -> None:
    merged = merge_findings(
        [
            finding(AgentType.SECURITY, confidence=0.9, severity=Severity.HIGH),
            finding(AgentType.QUALITY, confidence=0.4, severity=Severity.MEDIUM),
        ]
    )
    assert len(merged.findings) == 1
    kept = merged.findings[0]
    assert kept.confidence == 0.9
    assert kept.severity is Severity.HIGH
    assert kept.agreed_by == [AgentType.QUALITY]
    assert merged.duplicates_collapsed == 1


def test_duplicate_takes_the_worst_severity_claimed() -> None:
    """Two agents seeing one issue and disagreeing on severity: the higher reading is
    the safe one, and the disagreement stays visible in agreed_by."""
    merged = merge_findings(
        [
            finding(AgentType.QUALITY, confidence=0.9, severity=Severity.LOW),
            finding(AgentType.SECURITY, confidence=0.5, severity=Severity.CRITICAL),
        ]
    )
    assert merged.findings[0].severity is Severity.CRITICAL
    assert merged.findings[0].agreed_by == [AgentType.SECURITY]


def test_different_lines_stay_separate_and_are_logged_as_near_misses() -> None:
    merged = merge_findings(
        [
            finding(AgentType.SECURITY, line=10, confidence=0.8),
            finding(AgentType.QUALITY, line=12, confidence=0.7),
        ]
    )
    assert len(merged.findings) == 2
    assert merged.near_misses
    assert merged.near_misses[0]["agent"] == "quality"
    assert "within" in merged.near_misses[0]["reason"]


def test_distant_lines_are_not_near_misses() -> None:
    merged = merge_findings(
        [
            finding(AgentType.SECURITY, line=10),
            finding(AgentType.QUALITY, line=400),
        ]
    )
    assert merged.near_misses == []


def test_different_categories_at_one_line_stay_separate() -> None:
    merged = merge_findings(
        [
            finding(AgentType.SECURITY, line=10, category=Category.INJECTION),
            finding(AgentType.QUALITY, line=10, category=Category.CORRECTNESS),
        ]
    )
    assert len(merged.findings) == 2


def test_incomplete_propagates_through_a_merge() -> None:
    partial = finding(AgentType.SECURITY).model_copy(update={"incomplete": True})
    merged = merge_findings([partial, finding(AgentType.QUALITY)])
    assert merged.findings[0].incomplete is True
    assert merged.incomplete_passes >= 1


def test_findings_are_ordered_by_severity_then_confidence() -> None:
    merged = merge_findings(
        [
            finding(AgentType.DOCS, line=1, severity=Severity.INFO, confidence=0.9),
            finding(AgentType.SECURITY, line=2, severity=Severity.CRITICAL, confidence=0.4),
            finding(AgentType.QUALITY, line=3, severity=Severity.HIGH, confidence=0.8),
        ]
    )
    assert [f.severity for f in merged.findings] == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.INFO,
    ]


def test_severity_counts() -> None:
    counts = severity_counts(
        [
            finding(AgentType.SECURITY, line=1, severity=Severity.CRITICAL),
            finding(AgentType.QUALITY, line=2, severity=Severity.CRITICAL),
            finding(AgentType.DOCS, line=3, severity=Severity.LOW),
        ]
    )
    assert counts == {"CRITICAL": 2, "LOW": 1}


# --- overall confidence ------------------------------------------------------
def test_full_coverage_with_no_findings_is_confidently_clean() -> None:
    assert overall_confidence([], completed_passes=4, total_passes=4) == 1.0


def test_a_timed_out_pass_lowers_confidence_even_with_no_findings() -> None:
    """The point of folding coverage in: an incomplete review must not read as clean."""
    assert overall_confidence([], completed_passes=4, total_passes=4, truncated_passes=1) == 0.75


def test_confidence_is_capped_by_coverage() -> None:
    high = finding(AgentType.QUALITY, confidence=0.99)
    capped = overall_confidence(
        [high], completed_passes=4, total_passes=4, truncated_passes=2
    )
    assert capped == 0.5


def test_confidence_weights_toward_the_most_severe_finding() -> None:
    confident_minor = finding(AgentType.DOCS, line=1, severity=Severity.INFO, confidence=1.0)
    unsure_major = finding(AgentType.SECURITY, line=2, severity=Severity.CRITICAL, confidence=0.2)
    weighted = overall_confidence(
        [confident_minor, unsure_major], completed_passes=4, total_passes=4
    )
    # (1.0*1 + 0.2*5) / 6 = 0.333..., i.e. dominated by the critical finding's uncertainty.
    assert weighted == 0.333


def test_no_passes_means_no_confidence() -> None:
    assert overall_confidence([], completed_passes=0, total_passes=0) == 0.0


# --- selection ---------------------------------------------------------------
def test_selection_keeps_everything_under_the_cap() -> None:
    findings = [finding(AgentType.QUALITY, line=i) for i in range(1, 6)]
    assert len(select_postable(findings, max_comments=20)) == 5


def test_selection_preserves_critical_findings_past_the_cap() -> None:
    findings = [finding(AgentType.QUALITY, line=i, severity=Severity.LOW) for i in range(1, 20)]
    findings.append(
        finding(AgentType.SECURITY, line=90, severity=Severity.CRITICAL, confidence=0.9)
    )
    selected = select_postable(findings, max_comments=5)
    assert len(selected) == 5
    assert any(f.severity is Severity.CRITICAL for f in selected)


def test_selection_prefers_high_severity_when_trimming() -> None:
    findings = [
        finding(AgentType.DOCS, line=1, severity=Severity.INFO),
        finding(AgentType.QUALITY, line=2, severity=Severity.HIGH),
        finding(AgentType.TESTS, line=3, severity=Severity.MEDIUM),
    ]
    selected = select_postable(findings, max_comments=1)
    assert selected[0].severity is Severity.HIGH


def test_needs_attention_flags_critical_and_high_only() -> None:
    assert needs_attention([finding(AgentType.SECURITY, severity=Severity.CRITICAL)])
    assert needs_attention([finding(AgentType.QUALITY, severity=Severity.HIGH)])
    assert not needs_attention([finding(AgentType.DOCS, severity=Severity.INFO)])
