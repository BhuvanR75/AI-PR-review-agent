"""Enumerations shared by the contract, the schema and the API."""

from __future__ import annotations

from enum import StrEnum


class AgentType(StrEnum):
    SECURITY = "security"
    QUALITY = "quality"
    TESTS = "tests"
    DOCS = "docs"
    AGGREGATOR = "aggregator"


#: The four specialist passes, in a stable order. Born from how a senior reviews (L1).
SPECIALIST_AGENTS: tuple[AgentType, ...] = (
    AgentType.SECURITY,
    AgentType.QUALITY,
    AgentType.TESTS,
    AgentType.DOCS,
)


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


#: Higher is worse. Used for dedup tie-breaks and ordering, never for routing
#: decisions on its own -- CRITICAL routing is an explicit rule, not a comparison.
SEVERITY_RANK: dict[Severity, int] = {
    Severity.CRITICAL: 4,
    Severity.HIGH: 3,
    Severity.MEDIUM: 2,
    Severity.LOW: 1,
    Severity.INFO: 0,
}


class Category(StrEnum):
    INJECTION = "injection"
    SECRETS = "secrets"
    AUTH = "auth"
    DESERIALIZATION = "deserialization"
    CORRECTNESS = "correctness"
    LOGIC = "logic"
    COMPLEXITY = "complexity"
    STYLE = "style"
    MISSING_TEST = "missing-test"
    BRITTLE_TEST = "brittle-test"
    COVERAGE_GAP = "coverage-gap"
    MISSING_DOCS = "missing-docs"
    OUTDATED_DOCS = "outdated-docs"
    UNDOCUMENTED_API = "undocumented-api"
    OTHER = "other"


class ReviewStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    POSTED = "posted"
    REJECTED = "rejected"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class Decision(StrEnum):
    """The aggregator's routing decision, before any human sees it."""

    POST = "post"
    HUMAN_APPROVAL = "human_approval"
    ESCALATE = "escalate"


class HitlState(StrEnum):
    AWAITING = "awaiting"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class FeedbackKind(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    EDITED = "edited"
    DISPUTED = "disputed"


class EventType(StrEnum):
    SPAN_START = "span.start"
    SPAN_END = "span.end"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    DECISION = "decision"
    ESCALATION = "escalation"


class Outcome(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"
    CRITICAL_BLOCK = "critical_block"
    ESCALATED = "escalated"
    ERROR = "error"
    INCOMPLETE = "incomplete"
    OK = "ok"
