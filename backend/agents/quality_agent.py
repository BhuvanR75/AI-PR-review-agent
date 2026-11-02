"""Correctness pass. Asks one question: is the logic right?"""

from __future__ import annotations

from typing import ClassVar

from backend.agents.base_agent import BaseAgent
from backend.models.enums import AgentType


class QualityAgent(BaseAgent):
    agent_type: ClassVar[AgentType] = AgentType.QUALITY

    @property
    def concern(self) -> str:
        return "correctness, logic errors, edge cases, unnecessary complexity"
