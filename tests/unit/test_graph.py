"""Graph-level tests, run against the real LangGraph topology with the database layer
stubbed out.

Two things are being proved here that unit tests of the parts cannot prove:

  * the fan-out joins. All four specialists contribute to one aggregation, which is
    what `operator.add` reducers plus a single downstream node have to achieve.
  * a stalled pass degrades rather than hangs. The graph must complete with an
    incomplete result instead of never firing the join.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import backend.orchestrator.nodes as nodes_module
from backend.agents import build_specialists
from backend.integrations.demo_pr import DEMO_HEAD_SHA, DEMO_PR_NUMBER, DEMO_REPO
from backend.integrations.github_client import DryRunGitHubClient
from backend.models.enums import SPECIALIST_AGENTS, ReviewStatus
from backend.models.review import ReviewRequest
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.nodes import NodeDependencies
from backend.settings import Settings
from backend.tools.llm_client import FakeLLMClient, LLMResponse

# --- stubs -------------------------------------------------------------------
captured: dict[str, Any] = {}


class _Session:
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _fake_session_scope():  # type: ignore[no-untyped-def]
    yield _Session()


class FakeReviewRepository:
    #: Class-level, because the graph constructs a repository per node and the
    #: aggregator's writes are what the fan-out test needs to inspect.
    all_stored: list[Any] = []
    all_near_misses: list[Any] = []

    def __init__(self, session: Any) -> None:
        self.statuses: list[tuple[Any, dict[str, Any]]] = []
        self.stored: list[Any] = []
        self.near_misses: list[Any] = []
        captured["repo"] = self

    async def supersede_previous_heads(self, repo: str, pr_number: int, keep: str) -> list[str]:
        return []

    async def set_status(self, review_id: str, status: Any, **kwargs: Any) -> None:
        self.statuses.append((status, kwargs))

    async def store_findings(self, review_id: str, findings: list[Any]) -> list[Any]:
        self.stored.extend(findings)
        FakeReviewRepository.all_stored.extend(findings)
        return []

    async def delete_findings(self, review_id: str) -> int:
        """The aggregator clears before storing (replace, not append); the fake
        mirrors that by dropping what it has already accumulated."""
        cleared = len(self.stored)
        self.stored.clear()
        FakeReviewRepository.all_stored.clear()
        return cleared

    async def record_near_misses(self, review_id: str, misses: list[Any]) -> None:
        self.near_misses.extend(misses)
        FakeReviewRepository.all_near_misses.extend(misses)


class FakeHitlRepository:
    def __init__(self, session: Any) -> None:
        captured["hitl"] = self

    async def enqueue(self, review_id: str, reason: str) -> None:
        captured["enqueued"] = (review_id, reason)


class SlowLLM(FakeLLMClient):
    """A provider that never answers in time, for the degradation test."""

    async def complete(self, **kwargs: Any) -> LLMResponse:
        await asyncio.sleep(30)
        return await super().complete(**kwargs)  # pragma: no cover - never reached


@pytest.fixture
def stubbed_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nodes_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(nodes_module, "ReviewRepository", FakeReviewRepository)
    monkeypatch.setattr(nodes_module, "HitlRepository", FakeHitlRepository)
    captured.clear()
    FakeReviewRepository.all_stored = []
    FakeReviewRepository.all_near_misses = []


def build_engine(settings: Settings, llm: Any = None) -> LangGraphWorkflowEngine:
    deps = NodeDependencies(
        github=DryRunGitHubClient(settings),
        agents=build_specialists(
            settings=settings, llm=llm or FakeLLMClient(settings), budget=None
        ),
        memory=None,
        settings=settings,
    )
    return LangGraphWorkflowEngine(deps, settings=settings, checkpointer=InMemorySaver())


def request(review_id: str) -> ReviewRequest:
    return ReviewRequest(
        review_id=review_id,
        repo=DEMO_REPO,
        pr_number=DEMO_PR_NUMBER,
        head_sha=DEMO_HEAD_SHA,
    )


# --- tests -------------------------------------------------------------------
async def test_all_four_specialists_contribute_to_one_aggregation(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    engine = build_engine(isolated_env)
    outcome = await engine.run(request(review_id))

    stored = FakeReviewRepository.all_stored
    contributors = {f.agent_type for f in stored}
    assert contributors == set(SPECIALIST_AGENTS), "the join must see every branch"
    assert outcome.finding_count == len(stored)
    assert outcome.finding_count >= 4


async def test_review_waits_for_a_human_by_default(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    outcome = await build_engine(isolated_env).run(request(review_id))

    assert outcome.status is ReviewStatus.PENDING_APPROVAL
    assert outcome.decision == "escalate", "the demo diff contains a CRITICAL finding"
    assert captured["enqueued"][0] == review_id
    assert outcome.critical_findings


async def test_a_stalled_pass_degrades_instead_of_hanging(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    """The orchestration-deadlock defense: four agents that never answer must produce an
    incomplete review, not a workflow that never finishes."""
    settings = isolated_env.model_copy(update={"agent_timeout_seconds": 0.05})
    outcome = await build_engine(settings, llm=SlowLLM(settings)).run(request(review_id))

    assert outcome.status is ReviewStatus.PENDING_APPROVAL
    assert any("Timeout" in error for error in outcome.errors)
    assert outcome.overall_confidence == 0.0
    assert outcome.finding_count == 0
    repo: FakeReviewRepository = captured["repo"]
    assert repo.statuses, "the review must still reach a terminal state"


async def test_state_is_checkpointed_under_the_review_id(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    engine = build_engine(isolated_env)
    await engine.run(request(review_id))

    state = await engine.get_state(review_id)
    assert state is not None
    assert state["review_id"] == review_id
    assert state["decision"] == "escalate"


async def test_resume_returns_the_same_outcome(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    engine = build_engine(isolated_env)
    first = await engine.run(request(review_id))
    resumed = await engine.resume(request(review_id))

    assert resumed.review_id == first.review_id
    assert resumed.decision == first.decision
    assert resumed.finding_count == first.finding_count


async def test_a_rerun_gets_its_own_thread(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    """A re-run must not resume the previous attempt's checkpoint, or its findings would
    be appended to the ones it is meant to replace."""
    engine = build_engine(isolated_env)
    await engine.run(request(review_id))
    await engine.run(request(review_id), thread_id=f"{review_id}:attempt2")

    assert (await engine.get_state(f"{review_id}:attempt2")) is not None


async def test_every_pass_records_its_model_call(
    stubbed_persistence: None, patched_event_pool: Any, isolated_env: Settings, review_id: str
) -> None:
    await build_engine(isolated_env).run(request(review_id))

    llm_events = [
        row
        for row, query in zip(patched_event_pool.rows, patched_event_pool.executed, strict=True)
        if "INSERT INTO AGENT_EVENTS" in query
    ]
    assert len(llm_events) >= 4
    event_types = {row[5] for row in llm_events}
    assert "span.start" in event_types
    assert "llm.call" in event_types
