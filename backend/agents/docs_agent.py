"""Documentation pass. Asks one question: will the next reader understand this?"""

from __future__ import annotations

from typing import ClassVar

from backend.agents.base_agent import BaseAgent
from backend.models.enums import AgentType


class DocsAgent(BaseAgent):
    agent_type: ClassVar[AgentType] = AgentType.DOCS

    @property
    def concern(self) -> str:
        return "missing docstrings, comments made wrong by the change, undocumented APIs"
