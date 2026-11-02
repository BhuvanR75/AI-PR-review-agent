"""The Finding contract is the object every component depends on, so its validators are
tested as behaviour rather than as an implementation detail."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.models.enums import AgentType, Category, Severity
from backend.models.findings import Finding, FindingsPayload


def make_finding(**overrides: object) -> Finding:
    base: dict[str, object] = {
        "agent_type": AgentType.SECURITY,
        "severity": Severity.HIGH,
        "category": Category.INJECTION,
        "summary": "SQL built by string interpolation",
        "file_path": "billing/stripe.py",
        "line_start": 12,
        "line_end": 12,
        "confidence": 0.8,
        "rationale": "the statement is assembled from an f-string",
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


def test_confidence_must_be_a_probability() -> None:
    with pytest.raises(ValidationError):
        make_finding(confidence=1.4)
    with pytest.raises(ValidationError):
        make_finding(confidence=-0.1)


def test_rationale_is_required() -> None:
    """A finding without evidence cannot be audited or disputed, so it cannot exist."""
    with pytest.raises(ValidationError):
        make_finding(rationale="")


def test_file_path_is_normalized() -> None:
    assert make_finding(file_path="./backend/app.py").file_path == "backend/app.py"
    assert make_finding(file_path="b/backend/app.py").file_path == "b/backend/app.py"


def test_empty_file_path_rejected() -> None:
    with pytest.raises(ValidationError):
        make_finding(file_path="  ")


def test_line_range_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        make_finding(line_start=20, line_end=10)
    assert make_finding(line_start=10, line_end=20).line_end == 20


def test_line_end_requires_line_start() -> None:
    with pytest.raises(ValidationError):
        make_finding(line_start=None, line_end=7)


def test_line_less_finding_is_allowed() -> None:
    """A coverage gap with no specific line is legitimate; it just cannot be posted inline."""
    finding = make_finding(line_start=None, line_end=None)
    assert finding.line_start is None
    assert finding.locator == "billing/stripe.py"


def test_dedup_key_ignores_agent_but_not_location() -> None:
    security = make_finding(agent_type=AgentType.SECURITY, line_start=12)
    quality = make_finding(agent_type=AgentType.QUALITY, line_start=12)
    elsewhere = make_finding(line_start=40, line_end=40)
    assert security.dedup_key == quality.dedup_key
    assert security.dedup_key != elsewhere.dedup_key


def test_lineless_findings_share_a_dedup_bucket_per_category() -> None:
    first = make_finding(line_start=None, line_end=None, category=Category.MISSING_TEST)
    second = make_finding(line_start=None, line_end=None, category=Category.MISSING_TEST)
    assert first.dedup_key == second.dedup_key
    assert first.dedup_key.endswith(":0:missing-test")


def test_locator_renders_a_range() -> None:
    assert make_finding(line_start=4, line_end=9).locator == "billing/stripe.py:4-9"
    assert make_finding(line_start=4, line_end=4).locator == "billing/stripe.py:4"


def test_findings_payload_parses_a_model_response() -> None:
    payload = FindingsPayload.model_validate(
        {
            "findings": [
                {
                    "agent_type": "security",
                    "severity": "CRITICAL",
                    "category": "secrets",
                    "summary": "committed credential",
                    "file_path": "a.py",
                    "line_start": 3,
                    "confidence": 0.9,
                    "rationale": "long literal assigned to a credential-named variable",
                }
            ]
        }
    )
    assert payload.findings[0].severity is Severity.CRITICAL
    assert payload.findings[0].line_end is None


def test_severity_ranking_is_monotonic() -> None:
    from backend.models.enums import SEVERITY_RANK

    order = [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.LOW,
        Severity.INFO,
    ]
    ranks = [SEVERITY_RANK[s] for s in order]
    assert ranks == sorted(ranks, reverse=True)
