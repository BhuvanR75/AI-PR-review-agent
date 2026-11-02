"""Tools: the LLM boundary, model routing, sandboxing and capability scoping.

The study's phase 7 also calls for Docker sandboxing of untrusted code. That is
deliberately not here yet -- nothing in the current pipeline executes repository
code, so a sandbox would be an unused attack-surface reduction. It lands with the
first tool that needs to run code.
"""

from backend.tools.capability_scope import (
    DEFAULT_SCOPE,
    Capability,
    CapabilityScope,
    ScopeViolation,
    scope_for,
)
from backend.tools.diffparse import (
    AddedLine,
    changed_paths,
    is_test_path,
    parse_added_lines,
    parse_hunks,
    touched_tests,
)
from backend.tools.llm_client import (
    FakeLLMClient,
    LLMClient,
    LLMResponse,
    OpenAILLMClient,
    estimate_cost,
    extract_json,
    get_llm_client,
)
from backend.tools.model_router import ModelRouter, Route, get_model_router, reset_model_router
from backend.tools.tool_registry import ToolRegistry, ToolSpec, get_tool_registry

__all__ = [
    "DEFAULT_SCOPE",
    "AddedLine",
    "Capability",
    "CapabilityScope",
    "FakeLLMClient",
    "LLMClient",
    "LLMResponse",
    "ModelRouter",
    "OpenAILLMClient",
    "Route",
    "ScopeViolation",
    "ToolRegistry",
    "ToolSpec",
    "changed_paths",
    "estimate_cost",
    "extract_json",
    "get_llm_client",
    "get_model_router",
    "get_tool_registry",
    "is_test_path",
    "parse_added_lines",
    "parse_hunks",
    "reset_model_router",
    "scope_for",
    "touched_tests",
]
