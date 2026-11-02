"""The scope boundary is a security property, not a convention: a specialist must not
be able to post to GitHub even if its prompt or tool call is hijacked."""

from __future__ import annotations

import pytest

from backend.models.enums import SPECIALIST_AGENTS
from backend.tools.capability_scope import (
    OPERATOR_SCOPE,
    PUBLISHER_SCOPE,
    Capability,
    ScopeViolation,
    scope_for,
)
from backend.tools.tool_registry import get_tool_registry


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_specialists_cannot_post(agent) -> None:  # type: ignore[no-untyped-def]
    scope = scope_for(agent)
    assert not scope.allows(Capability.POST_REVIEW)
    with pytest.raises(ScopeViolation):
        scope.require(Capability.POST_REVIEW)


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_specialists_can_do_their_job(agent) -> None:  # type: ignore[no-untyped-def]
    scope = scope_for(agent)
    assert scope.allows(Capability.READ_REPO)
    assert scope.allows(Capability.CALL_LLM)
    assert scope.allows(Capability.WRITE_EVENTS)


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_specialists_cannot_execute_code(agent) -> None:  # type: ignore[no-untyped-def]
    """Nothing in the pipeline runs repository code, so nothing is allowed to."""
    assert not scope_for(agent).allows(Capability.EXECUTE_CODE)


def test_aggregator_cannot_post_either() -> None:
    assert not scope_for("aggregator").allows(Capability.POST_REVIEW)


def test_publisher_and_operator_can_post() -> None:
    assert PUBLISHER_SCOPE.allows(Capability.POST_REVIEW)
    assert OPERATOR_SCOPE.allows(Capability.POST_REVIEW)
    assert OPERATOR_SCOPE.allows(Capability.READ_EVENTS)


def test_operator_cannot_execute_code() -> None:
    assert not OPERATOR_SCOPE.allows(Capability.EXECUTE_CODE)


def test_unknown_scope_falls_back_to_the_most_restrictive() -> None:
    scope = scope_for("mystery-agent")
    assert scope.name == "specialist"


def test_scope_violation_names_both_sides() -> None:
    with pytest.raises(ScopeViolation) as exc:
        scope_for("security").require(Capability.POST_REVIEW)
    assert exc.value.scope == "specialist"
    assert exc.value.capability is Capability.POST_REVIEW
    assert "post_review" in str(exc.value)


# --- tool registry -----------------------------------------------------------
def test_post_review_is_the_only_dangerous_publishing_tool() -> None:
    registry = get_tool_registry()
    spec = registry.get("post_review")
    assert spec.capability is Capability.POST_REVIEW
    assert spec.dangerous


def test_registry_refuses_to_hand_a_specialist_the_publishing_tool() -> None:
    registry = get_tool_registry()
    with pytest.raises(ScopeViolation):
        registry.require("post_review", scope_for("security"))
    assert registry.require("post_review", OPERATOR_SCOPE).name == "post_review"


def test_registry_describes_itself_for_operators() -> None:
    described = {item["name"]: item for item in get_tool_registry().describe()}
    assert "retrieve_code_context" in described
    assert described["retrieve_code_context"]["capability"] == "read_repo"
    assert described["retrieve_code_context"]["dangerous"] is False


def test_unknown_tool_raises() -> None:
    with pytest.raises(KeyError):
        get_tool_registry().get("nonexistent")
