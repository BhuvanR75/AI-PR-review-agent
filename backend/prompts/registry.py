"""Prompt registry.

Prompts are versioned and content-hashed for one reason: the audit trail has to be
able to say *which prompt produced this finding*. A model name alone is not
reproducible, because prompts change far more often than models do -- and a finding
raised by a prompt that no longer exists is undebuggable.

The hash is of the file's exact bytes, so an edit is visible even if someone forgets
to bump VERSION.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from backend.models.enums import AgentType

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

DEFAULT_VERSION = "v1"


@dataclass(frozen=True)
class PromptTemplate:
    agent: AgentType
    version: str
    sha256: str
    system: str
    path: str

    @property
    def short_hash(self) -> str:
        return self.sha256[:12]


class PromptRegistry:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or TEMPLATES_DIR
        self._cache: dict[AgentType, PromptTemplate] = {}

    def _load(self, agent: AgentType) -> PromptTemplate:
        path = self.directory / f"{agent.value}.txt"
        if not path.exists():
            raise FileNotFoundError(f"prompt template missing for agent '{agent.value}': {path}")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        version = DEFAULT_VERSION
        for line in text.splitlines():
            if line.startswith("VERSION:"):
                version = line.split(":", 1)[1].strip()
                break
        return PromptTemplate(
            agent=agent,
            version=version,
            sha256=hashlib.sha256(raw).hexdigest(),
            system=text,
            path=str(path),
        )

    def get(self, agent: AgentType | str) -> PromptTemplate:
        key = AgentType(str(agent))
        if key not in self._cache:
            self._cache[key] = self._load(key)
        return self._cache[key]

    def all(self) -> dict[str, PromptTemplate]:
        return {agent.value: self.get(agent) for agent in AgentType if self._exists(agent)}

    def _exists(self, agent: AgentType) -> bool:
        return (self.directory / f"{agent.value}.txt").exists()


@lru_cache
def get_prompt_registry() -> PromptRegistry:
    return PromptRegistry()


def reset_prompt_registry() -> None:
    get_prompt_registry.cache_clear()
