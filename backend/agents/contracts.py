"""The agent-facing contract, re-exported in one place.

Components talk in these types. If something needs a new field to cross a boundary,
it is added here rather than passed as a dict, because the aggregator, the HITL gate,
the GitHub poster and the audit trail all read the same shape.
"""

from backend.models.findings import Finding, FindingsPayload, SpecialistResult
from backend.models.review import ChangedFile, ReviewOutcome, ReviewRequest

from .base_agent import BaseAgent, SpecialistInput, anchor_findings

__all__ = [
    "BaseAgent",
    "ChangedFile",
    "Finding",
    "FindingsPayload",
    "ReviewOutcome",
    "ReviewRequest",
    "SpecialistInput",
    "SpecialistResult",
    "anchor_findings",
]
