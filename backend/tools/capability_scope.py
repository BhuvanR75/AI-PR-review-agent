"""Capability scoping.

The useful property here is a negative one: a specialist reasoning about a diff
*must not* be able to post to GitHub, even if a prompt is hijacked or a tool call is
misconstructed. Reading code, calling a model, and writing events are all a
specialist needs. Posting is reserved for the aggregator, and posting after human
approval is reserved for the HITL path.

That is a boundary enforced in code rather than in a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.models.enums import AgentType


class Capability(StrEnum):
    READ_REPO = "read_repo"
    CALL_LLM = "call_llm"
    WRITE_EVENTS = "write_events"
    READ_EVENTS = "read_events"
    POST_REVIEW = "post_review"
    EXECUTE_CODE = "execute_code"


class ScopeViolation(PermissionError):
    def __init__(self, scope: str, capability: Capability) -> None:
        super().__init__(f"scope '{scope}' does not grant '{capability}'")
        self.scope = scope
        self.capability = capability


@dataclass(frozen=True)
class CapabilityScope:
    name: str
    capabilities: frozenset[Capability]
    notes: dict[Capability, str] = field(default_factory=dict)

    def allows(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def require(self, capability: Capability) -> None:
        if not self.allows(capability):
            raise ScopeViolation(self.name, capability)

    def describe(self) -> str:
        return ", ".join(sorted(c.value for c in self.capabilities))


#: A specialist read-and-reason scope. Notably absent: POST_REVIEW and EXECUTE_CODE.
SPECIALIST_SCOPE = CapabilityScope(
    name="specialist",
    capabilities=frozenset({Capability.READ_REPO, Capability.CALL_LLM, Capability.WRITE_EVENTS}),
)

#: The aggregator decides and routes; it still cannot post to GitHub directly.
AGGREGATOR_SCOPE = CapabilityScope(
    name="aggregator",
    capabilities=frozenset(
        {Capability.READ_REPO, Capability.CALL_LLM, Capability.WRITE_EVENTS, Capability.READ_EVENTS}
    ),
)

#: Only the human-approved publication path holds this.
PUBLISHER_SCOPE = CapabilityScope(
    name="publisher",
    capabilities=frozenset(
        {
            Capability.READ_REPO,
            Capability.WRITE_EVENTS,
            Capability.READ_EVENTS,
            Capability.POST_REVIEW,
        }
    ),
)

#: The approval console reads everything and writes decisions.
OPERATOR_SCOPE = CapabilityScope(
    name="operator",
    capabilities=frozenset(
        {
            Capability.READ_REPO,
            Capability.READ_EVENTS,
            Capability.WRITE_EVENTS,
            Capability.POST_REVIEW,
        }
    ),
)

DEFAULT_SCOPE = SPECIALIST_SCOPE

_SCOPES: dict[str, CapabilityScope] = {
    AgentType.SECURITY.value: SPECIALIST_SCOPE,
    AgentType.QUALITY.value: SPECIALIST_SCOPE,
    AgentType.TESTS.value: SPECIALIST_SCOPE,
    AgentType.DOCS.value: SPECIALIST_SCOPE,
    AgentType.AGGREGATOR.value: AGGREGATOR_SCOPE,
    PUBLISHER_SCOPE.name: PUBLISHER_SCOPE,
    OPERATOR_SCOPE.name: OPERATOR_SCOPE,
}


def scope_for(agent: AgentType | str) -> CapabilityScope:
    return _SCOPES.get(str(agent), DEFAULT_SCOPE)
