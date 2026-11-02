"""The four specialists and their shared base."""

from __future__ import annotations

from typing import Any

from backend.agents.base_agent import BaseAgent, SpecialistInput, anchor_findings
from backend.agents.docs_agent import DocsAgent
from backend.agents.quality_agent import QualityAgent
from backend.agents.security_agent import SecurityAgent
from backend.agents.test_agent import TestsAgent
from backend.models.enums import SPECIALIST_AGENTS, AgentType

AGENT_CLASSES: dict[AgentType, type[BaseAgent]] = {
    AgentType.SECURITY: SecurityAgent,
    AgentType.QUALITY: QualityAgent,
    AgentType.TESTS: TestsAgent,
    AgentType.DOCS: DocsAgent,
}

#: The one question each pass asks, held as data so reporting never has to
#: instantiate an agent (and with it an LLM client and a circuit breaker) to describe
#: itself.
AGENT_CONCERNS: dict[AgentType, str] = {
    AgentType.SECURITY: "could this be exploited?",
    AgentType.QUALITY: "is the logic right?",
    AgentType.TESTS: "what is now unverified?",
    AgentType.DOCS: "will the next reader understand this?",
}


def build_agent(agent_type: AgentType, **kwargs: Any) -> BaseAgent:
    try:
        cls = AGENT_CLASSES[AgentType(str(agent_type))]
    except KeyError as exc:
        raise KeyError(f"no specialist registered for '{agent_type}'") from exc
    return cls(**kwargs)


def build_specialists(**kwargs: Any) -> dict[AgentType, BaseAgent]:
    """One instance per specialist, sharing the same dependencies.

    Shares them deliberately: one LLM client means one circuit breaker per provider
    rather than four, so a provider outage opens the circuit instead of being probed
    four times in parallel.
    """
    return {agent: build_agent(agent, **kwargs) for agent in SPECIALIST_AGENTS}


__all__ = [
    "AGENT_CLASSES",
    "AGENT_CONCERNS",
    "BaseAgent",
    "DocsAgent",
    "QualityAgent",
    "SecurityAgent",
    "SpecialistInput",
    "TestsAgent",
    "anchor_findings",
    "build_agent",
    "build_specialists",
]
