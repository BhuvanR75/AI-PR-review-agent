"""The tool registry.

Every capability the system has is registered here with the capability it needs, and
invocation goes through the scope check. The registry is the single place where
"what can this agent do" is answerable, which is what makes the answer auditable
rather than inferred from call sites.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from backend.tools.capability_scope import Capability, CapabilityScope, ScopeViolation

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    capability: Capability
    dangerous: bool = False
    requires_network: bool = False
    handler: Handler | None = None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"tool '{spec.name}' already registered")
        self._tools[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"unknown tool '{name}'") from exc

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def require(self, name: str, scope: CapabilityScope) -> ToolSpec:
        spec = self.get(name)
        scope.require(spec.capability)
        return spec

    async def invoke(
        self,
        name: str,
        scope: CapabilityScope,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        spec = self.require(name, scope)
        if spec.handler is None:
            raise RuntimeError(f"tool '{name}' has no handler bound")
        logger.info("tool invoked", extra={"context": {"tool": name, "scope": scope.name}})
        return await spec.handler(*args, **kwargs)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": spec.name,
                "capability": spec.capability.value,
                "dangerous": spec.dangerous,
                "network": spec.requires_network,
                "description": spec.description,
            }
            for spec in self.all()
        ]


def _build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="retrieve_code_context",
            description="Hybrid vector+keyword search over code_chunks for a repo.",
            capability=Capability.READ_REPO,
        )
    )
    registry.register(
        ToolSpec(
            name="fetch_pull_request",
            description="Read PR metadata, changed files and the unified diff.",
            capability=Capability.READ_REPO,
            requires_network=True,
        )
    )
    registry.register(
        ToolSpec(
            name="post_review",
            description="Publish the aggregated review to the pull request.",
            capability=Capability.POST_REVIEW,
            dangerous=True,
            requires_network=True,
        )
    )
    registry.register(
        ToolSpec(
            name="emit_event",
            description="Append one row to the events spine.",
            capability=Capability.WRITE_EVENTS,
        )
    )
    registry.register(
        ToolSpec(
            name="read_trace",
            description="Read the time-ordered event stream for a review.",
            capability=Capability.READ_EVENTS,
        )
    )
    registry.register(
        ToolSpec(
            name="run_sandboxed_command",
            description="Execute a command inside an isolated container (not yet implemented).",
            capability=Capability.EXECUTE_CODE,
            dangerous=True,
        )
    )
    return registry


@lru_cache
def get_tool_registry() -> ToolRegistry:
    return _build_default_registry()


def reset_tool_registry() -> None:
    get_tool_registry.cache_clear()


__all__ = ["ScopeViolation", "ToolRegistry", "ToolSpec", "get_tool_registry", "reset_tool_registry"]
