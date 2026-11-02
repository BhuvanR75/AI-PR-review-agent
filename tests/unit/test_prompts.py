"""Prompts are the part of a review that changes most often, so their identity is
tested as data: the audit trail can only name a prompt if the registry is stable and
distinguishes one revision from another."""

from __future__ import annotations

import pytest

from backend.models.enums import SPECIALIST_AGENTS, AgentType
from backend.prompts.registry import PromptRegistry
from backend.tools.llm_client import AGENT_MARKER


@pytest.fixture
def registry() -> PromptRegistry:
    return PromptRegistry()


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_every_specialist_has_a_template(registry: PromptRegistry, agent: AgentType) -> None:
    template = registry.get(agent)
    assert template.agent is agent
    assert template.system.strip()
    assert template.version.startswith("v")


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_template_declares_which_agent_it_is_for(
    registry: PromptRegistry, agent: AgentType
) -> None:
    """The offline provider identifies the asking agent from this marker, so a template
    without it would silently return no findings."""
    line = next(
        candidate
        for candidate in registry.get(agent).system.splitlines()
        if candidate.strip().startswith(AGENT_MARKER)
    )
    assert line.split(AGENT_MARKER, 1)[1].strip() == agent.value


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_template_demands_a_rationale_and_a_line(
    registry: PromptRegistry, agent: AgentType
) -> None:
    system = registry.get(agent).system
    assert "rationale" in system
    assert "line_start" in system
    assert "confidence" in system


@pytest.mark.parametrize("agent", SPECIALIST_AGENTS)
def test_template_allows_an_empty_result(registry: PromptRegistry, agent: AgentType) -> None:
    """A prompt that never permits 'nothing here' is a prompt that invents findings."""
    assert "empty" in registry.get(agent).system.lower()


def test_templates_are_distinct(registry: PromptRegistry) -> None:
    hashes = {registry.get(agent).sha256 for agent in SPECIALIST_AGENTS}
    assert len(hashes) == len(SPECIALIST_AGENTS)


def test_hash_is_stable_across_reads(registry: PromptRegistry) -> None:
    first = registry.get(AgentType.SECURITY).sha256
    second = PromptRegistry().get(AgentType.SECURITY).sha256
    assert first == second
    assert len(first) == 64


def test_hash_changes_when_content_changes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Edits must be visible even if someone forgets to bump the version."""
    (tmp_path / "security.txt").write_text(
        "VERSION: v1\nAGENT: security\noriginal\n", encoding="utf-8"
    )
    before = PromptRegistry(tmp_path).get(AgentType.SECURITY)
    (tmp_path / "security.txt").write_text(
        "VERSION: v1\nAGENT: security\nedited\n", encoding="utf-8"
    )
    after = PromptRegistry(tmp_path).get(AgentType.SECURITY)
    assert before.sha256 != after.sha256
    assert before.version == after.version


def test_version_is_read_from_the_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    (tmp_path / "docs.txt").write_text("VERSION: v7\nAGENT: docs\n", encoding="utf-8")
    assert PromptRegistry(tmp_path).get(AgentType.DOCS).version == "v7"


def test_missing_template_is_an_error_not_a_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(FileNotFoundError):
        PromptRegistry(tmp_path).get(AgentType.QUALITY)


def test_short_hash_is_a_prefix(registry: PromptRegistry) -> None:
    template = registry.get(AgentType.TESTS)
    assert template.sha256.startswith(template.short_hash)
    assert len(template.short_hash) == 12
