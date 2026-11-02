"""The spine has to be both best-effort (never break a review) and trustworthy (never
silently lose a row). Both halves are tested, because getting only the first is how an
audit trail quietly develops holes."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import backend.observability.events as events_module
from backend.models.enums import AgentType, Category, EventType, Severity
from backend.models.findings import Finding
from backend.models.review import AgentEvent
from backend.observability.audit import explain_finding, run_summary
from backend.observability.events import (
    Span,
    emit_agent_event,
    get_emit_failures,
    read_trace,
)


@pytest.fixture(autouse=True)
def _reset_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events_module, "emit_failures", 0)


async def test_event_row_carries_the_cost_attribution_columns(fake_pool: Any) -> None:
    await emit_agent_event(
        agent="security",
        event_type=EventType.LLM_CALL,
        review_id="11111111-1111-1111-1111-111111111111",
        model="gpt-4o-mini",
        tokens_in=100,
        tokens_out=40,
        cost_usd=0.0001,
        latency_ms=1200,
        outcome="ok",
        confidence=0.8,
        payload={"prompt_hash": "abc"},
        pool=fake_pool,
    )
    (row,) = fake_pool.rows
    assert row[2] == "security"
    assert row[5] == "llm.call"
    assert row[6] == "gpt-4o-mini"
    assert row[7] == 100
    assert row[9] == 0.0001
    assert row[10] == 1200
    assert row[13] == {"prompt_hash": "abc"}


async def test_a_failing_spine_write_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing a log line is better than losing the review, but it must be counted."""

    class Broken:
        async def execute(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("spine down")

    monkeypatch.setattr(events_module, "get_pool", lambda: Broken())
    await emit_agent_event(agent="security", event_type=EventType.SPAN_START)
    assert get_emit_failures() == 1


async def test_span_brackets_its_work_with_latency(patched_event_pool: Any) -> None:
    explicit = Span(agent="quality", review_id="22222222-2222-2222-2222-222222222222")
    async with explicit as active:
        active.confidence = 0.7
        await active.event(EventType.LLM_CALL, cost_usd=0.002, model="m")

    args = [
        row
        for _query, row in zip(patched_event_pool.executed, patched_event_pool.rows, strict=True)
    ]
    kinds = [row[5] for row in args]
    assert kinds == ["span.start", "llm.call", "span.end"]
    assert args[-1][10] is not None, "span.end records elapsed milliseconds"
    assert args[-1][12] == 0.7


async def test_span_shares_one_span_id_across_its_events(patched_event_pool: Any) -> None:
    async with Span(agent="docs", review_id="33333333-3333-3333-3333-333333333333") as active:
        await active.event(EventType.TOOL_CALL)

    span_ids = {row[3] for row in patched_event_pool.rows}
    assert len(span_ids) == 1


async def test_an_exception_marks_the_span_errored_and_propagates(
    patched_event_pool: Any,
) -> None:
    with pytest.raises(ValueError):
        async with Span(agent="docs"):
            raise ValueError("boom")

    last = patched_event_pool.rows[-1]
    assert last[5] == "span.end"
    assert last[11] == "error"


async def test_trace_reads_back_in_time_order(fake_pool: Any) -> None:
    now = datetime.now(UTC)
    fake_pool.fetch_rows = [
        {
            "ts": now,
            "review_id": "44444444-4444-4444-4444-444444444444",
            "agent": "security",
            "span_id": "55555555-5555-5555-5555-555555555555",
            "parent_span": None,
            "event_type": "llm.call",
            "model": "gpt-4o-mini",
            "tokens_in": 10,
            "tokens_out": 5,
            "cost_usd": 0.001,
            "latency_ms": 900,
            "outcome": "ok",
            "confidence": 0.9,
            "payload": {"prompt_hash": "deadbeef"},
        }
    ]
    events = await read_trace("44444444-4444-4444-4444-444444444444", pool=fake_pool)
    assert len(events) == 1
    assert events[0].agent == "security"
    assert events[0].cost_usd == pytest.approx(0.001)
    assert events[0].payload == {"prompt_hash": "deadbeef"}
    assert "ORDER BY TS" in fake_pool.executed[0]


# --- audit -------------------------------------------------------------------
def event(**overrides: Any) -> AgentEvent:
    base: dict[str, Any] = {
        "ts": datetime.now(UTC),
        "review_id": "66666666-6666-6666-6666-666666666666",
        "agent": AgentType.SECURITY,
        "span_id": "77777777-7777-7777-7777-777777777777",
        "event_type": EventType.LLM_CALL,
        "model": "gpt-4o-mini",
        "cost_usd": 0.003,
        "latency_ms": 1500,
        "payload": {"prompt_version": "v1", "prompt_hash": "cafe"},
    }
    base.update(overrides)
    return AgentEvent(**base)


def finding() -> Finding:
    return Finding(
        agent_type=AgentType.SECURITY,
        severity=Severity.HIGH,
        category=Category.INJECTION,
        summary="interpolated SQL",
        file_path="stripe.py",
        line_start=9,
        confidence=0.8,
        rationale="f-string reaches execute()",
    )


def test_explanation_names_the_model_and_prompt() -> None:
    """A disputed finding has to be reproducible, so this is the minimum provenance."""
    explanation = explain_finding(finding(), [event()])
    assert explanation.model == "gpt-4o-mini"
    assert explanation.prompt_version == "v1"
    assert explanation.prompt_hash == "cafe"
    assert explanation.cost_usd == pytest.approx(0.003)
    assert explanation.complete
    assert explanation.caveat is None


def test_explanation_admits_when_it_cannot_prove_provenance() -> None:
    explanation = explain_finding(finding(), [])
    assert not explanation.complete
    assert explanation.caveat is not None
    assert "unverified" in explanation.caveat


def test_explanation_ignores_other_agents_events() -> None:
    other = event(agent=AgentType.DOCS, model="other-model")
    explanation = explain_finding(finding(), [other])
    assert explanation.model is None


def test_explanation_records_independent_agreement() -> None:
    agreed = finding().model_copy(update={"agreed_by": [AgentType.QUALITY]})
    explanation = explain_finding(agreed, [event()])
    assert explanation.agreeing_agents == ["quality"]
    assert any("independently raised" in line for line in explanation.evidence)


def test_run_summary_reports_incomplete_passes_and_errors() -> None:
    events = [
        event(event_type=EventType.SPAN_END, outcome="incomplete", agent=AgentType.TESTS),
        event(
            event_type=EventType.LLM_CALL,
            outcome="error",
            agent=AgentType.DOCS,
            payload={"error": "circuit open"},
        ),
    ]
    errors, incomplete = run_summary(events)
    assert incomplete == 1
    assert any("tests" in e and "incomplete" in e for e in errors)
    assert any("circuit open" in e for e in errors)


def test_run_summary_is_quiet_for_a_clean_run() -> None:
    errors, incomplete = run_summary([event(event_type=EventType.SPAN_END, outcome="ok")])
    assert errors == []
    assert incomplete == 0
