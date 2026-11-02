"""Observability: the events spine and its three consumers."""

from backend.observability.audit import FindingExplanation, explain_finding, explain_review
from backend.observability.events import (
    Span,
    count_events,
    emit_agent_event,
    get_emit_failures,
    read_trace,
    span,
)
from backend.observability.logging import configure_logging
from backend.observability.tracing import (
    bind_review,
    bind_span,
    current_agent,
    current_review_id,
    current_span_id,
)

__all__ = [
    "FindingExplanation",
    "Span",
    "bind_review",
    "bind_span",
    "configure_logging",
    "count_events",
    "current_agent",
    "current_review_id",
    "current_span_id",
    "emit_agent_event",
    "explain_finding",
    "explain_review",
    "get_emit_failures",
    "read_trace",
    "span",
]
