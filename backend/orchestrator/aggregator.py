"""Aggregation: merge, deduplicate, score and select. All deterministic.

This is a deliberate architectural choice worth stating: the aggregator does not call
an LLM. Merging four lists and computing a score must produce the same answer for the
same input every time, because the result routes work to humans and eventually
authorizes posting. A severity score is exactly the case the study's design template
warns about -- "if the output must be the same every time given the same input, it
must be deterministic".

Everything here is a pure function of the findings, so the routing policy can be
tested without a database, an orchestrator or a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.enums import SEVERITY_RANK, SPECIALIST_AGENTS, AgentType, Severity
from backend.models.findings import Finding

#: How close two findings in the same file and category must be to count as a
#: near-miss worth logging. Chosen to catch "same bug, reported a few lines apart"
#: without flagging unrelated issues in a long file.
NEAR_MISS_LINE_WINDOW = 5


@dataclass
class MergeResult:
    findings: list[Finding] = field(default_factory=list)
    near_misses: list[dict[str, str]] = field(default_factory=list)
    duplicates_collapsed: int = 0
    incomplete_passes: int = 0

    @property
    def severity_counts(self) -> dict[str, int]:
        return severity_counts(self.findings)


def merge_findings(findings: list[Finding]) -> MergeResult:
    """Collapse findings that multiple specialists raised at the same anchor.

    The kept finding is the highest-confidence one, escalated to the worst severity
    any agent claimed -- two agents seeing the same issue and disagreeing on severity
    means the higher severity is the safe reading, and the disagreement is recorded
    in `agreed_by` rather than averaged away.
    """
    result = MergeResult()
    by_key: dict[str, list[Finding]] = {}
    for finding in findings:
        by_key.setdefault(finding.dedup_key, []).append(finding)

    for group in by_key.values():
        ordered = sorted(
            group,
            key=lambda f: (
                -f.confidence,
                -SEVERITY_RANK[f.severity],
                SPECIALIST_AGENTS.index(f.agent_type)
                if f.agent_type in SPECIALIST_AGENTS
                else len(SPECIALIST_AGENTS),
            ),
        )
        winner = ordered[0]
        others = list(ordered[1:])
        agreeing: list[AgentType] = sorted(
            {f.agent_type for f in others},
            key=lambda a: SPECIALIST_AGENTS.index(a) if a in SPECIALIST_AGENTS else 99,
        )
        worst = max(group, key=lambda f: SEVERITY_RANK[f.severity]).severity

        merged = winner.model_copy(
            update={
                "severity": worst
                if SEVERITY_RANK[worst] > SEVERITY_RANK[winner.severity]
                else winner.severity,
                "agreed_by": agreeing,
                "incomplete": any(f.incomplete for f in group),
            }
        )
        result.findings.append(merged)
        result.duplicates_collapsed += len(others)
        result.incomplete_passes += sum(1 for f in group if f.incomplete)

    result.findings.sort(key=_severity_then_confidence_desc)
    result.near_misses = _collect_near_misses(result.findings)
    return result


def _collect_near_misses(merged: list[Finding]) -> list[dict[str, str]]:
    """Pairs of *kept* findings that look like the same issue but were not merged.

    This has to run across dedup keys rather than inside one, because two findings in
    the same key have the same anchor by definition -- so a same-key comparison can
    never find a near miss. The pair that matters is two agents reporting one root
    cause a few lines apart, which is exactly what the coarse key does not catch.
    """
    misses: list[dict[str, str]] = []
    for index, first in enumerate(merged):
        for second in merged[index + 1 :]:
            if not _is_near_miss(first, second):
                continue
            misses.append(
                {
                    "kept": first.dedup_key,
                    "dropped": second.dedup_key,
                    "agent": second.agent_type.value,
                    "reason": (
                        "same file and category within "
                        f"{NEAR_MISS_LINE_WINDOW} lines but a different anchor"
                    ),
                }
            )
    return misses


def _is_near_miss(first: Finding, second: Finding) -> bool:
    if first.file_path != second.file_path or first.category is not second.category:
        return False
    if first.line_start is None or second.line_start is None:
        return False
    if first.line_start == second.line_start:
        return False  # same anchor means they already merged
    return abs(first.line_start - second.line_start) <= NEAR_MISS_LINE_WINDOW


def _severity_then_confidence_desc(finding: Finding) -> tuple[int, float, str, int]:
    return (
        -SEVERITY_RANK[finding.severity],
        -finding.confidence,
        finding.file_path,
        finding.line_start or 0,
    )


def severity_counts(findings: list[Finding]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1
    return counts


def overall_confidence(
    findings: list[Finding],
    *,
    completed_passes: int,
    total_passes: int,
    truncated_passes: int = 0,
) -> float:
    """One number, for the gate. Not a calibrated probability -- say so plainly.

    It combines two different things, and it is worth being explicit about why:

      * coverage -- how many passes actually finished. A timed-out security pass
        means the review is confidently *incomplete*, which must lower the score even
        though nothing in the findings says so.
      * agreement -- the confidence the passes reported, weighted toward the most
        severe finding, because that is the one whose error matters most.

    LLM self-reported confidence is not calibrated, so this value is a routing hint
    until approval outcomes give us the labels to calibrate against. With
    HITL_MODE=always it gates nothing at all, which is the honest starting position.
    """
    if total_passes <= 0:
        return 0.0
    coverage = max(0.0, (completed_passes - truncated_passes) / total_passes)

    if not findings:
        return round(coverage, 3)

    weights = [SEVERITY_RANK[f.severity] + 1 for f in findings]
    weighted = sum(f.confidence * w for f, w in zip(findings, weights, strict=True)) / sum(weights)
    return round(min(weighted, coverage), 3)


def select_postable(findings: list[Finding], *, max_comments: int) -> list[Finding]:
    """Trim to the findings worth a reviewer's attention (the L0 selectivity posture).

    CRITICAL findings are never trimmed -- they are the reason the count limit can be
    exceeded. Everything else is ranked by severity then confidence, so what survives
    is the highest-value subset rather than the first N.
    """
    if len(findings) <= max_comments:
        return list(findings)

    critical = [f for f in findings if f.severity is Severity.CRITICAL]
    rest = sorted(
        (f for f in findings if f.severity is not Severity.CRITICAL),
        key=_severity_then_confidence_desc,
    )
    remaining = max(0, max_comments - len(critical))
    return critical + rest[:remaining]


def needs_attention(findings: list[Finding]) -> bool:
    return any(f.severity in {Severity.CRITICAL, Severity.HIGH} for f in findings)
