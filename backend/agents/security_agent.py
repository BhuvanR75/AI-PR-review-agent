"""Security pass. Asks one question: could this be exploited?

The class is a declaration, not an algorithm. Its behaviour lives in three places by
design -- the prompt template (what to look for), the capability scope (what it may
do), and `anchor_findings` in the base class (what it is allowed to claim). Anything
security-specific implemented as a code branch would be untestable and invisible in
the audit trail, where the prompt version already records the reasoning.
"""

from __future__ import annotations

from typing import ClassVar

from backend.agents.base_agent import BaseAgent
from backend.models.enums import AgentType


class SecurityAgent(BaseAgent):
    agent_type: ClassVar[AgentType] = AgentType.SECURITY

    @property
    def concern(self) -> str:
        return "injection, secrets, auth bypass, unsafe deserialization"
