"""Model routing per agent.

Routing exists so cost can be shaped without rewriting prompts: the docs pass can
run on a cheap model while security, where a miss is dangerous, runs on something
stronger. Phase 5 of the study calls for exactly this, and the default here keeps
one model everywhere until the economics justify splitting them.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from backend.models.enums import AgentType
from backend.settings import Settings, get_settings

DEFAULT_MODEL = "gpt-4o-mini"

#: Output budgets. Tuned to the shape of each pass: security findings are few and
#: terse, quality findings are many, tests and docs are medium.
MAX_TOKENS_BY_AGENT: dict[str, int] = {
    AgentType.SECURITY.value: 1500,
    AgentType.QUALITY.value: 2000,
    AgentType.TESTS.value: 1500,
    AgentType.DOCS.value: 1200,
    AgentType.AGGREGATOR.value: 800,
}


@dataclass(frozen=True)
class Route:
    agent: str
    model: str
    max_tokens: int


class ModelRouter:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def model_for(self, agent: AgentType | str) -> str:
        key = str(agent)
        return self.settings.model_route_map.get(key, DEFAULT_MODEL)

    def max_tokens_for(self, agent: AgentType | str) -> int:
        return MAX_TOKENS_BY_AGENT.get(str(agent), 1500)

    def route(self, agent: AgentType | str) -> Route:
        return Route(
            agent=str(agent),
            model=self.model_for(agent),
            max_tokens=self.max_tokens_for(agent),
        )

    def snapshot(self) -> dict[str, Route]:
        return {a.value: self.route(a) for a in AgentType}


@lru_cache
def get_model_router() -> ModelRouter:
    return ModelRouter()


def reset_model_router() -> None:
    get_model_router.cache_clear()
