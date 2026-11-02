"""The full loop against a real database: webhook-shaped request in, human-approved
review posted, with every step recorded on the spine.

This is the milestone gate. It uses the real graph, the real repositories, the real
events table and the real dry-run GitHub client, with only the model and the embedder
faked -- so the parts that are being trusted in production are the parts being
exercised here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from backend.agents import build_specialists
from backend.database.pg import create_pool
from backend.database.repository import HitlRepository, ReviewRepository
from backend.database.session import session_scope
from backend.economics.budget import BudgetGuard
from backend.economics.cost_repository import CostRepository
from backend.hitl.decisions import HitlService
from backend.integrations.demo_pr import DEMO_HEAD_SHA, DEMO_PR_NUMBER, DEMO_REPO
from backend.integrations.github_client import DryRunGitHubClient
from backend.memory.code_memory import ChunkInput, CodeMemory
from backend.memory.embedder import FakeEmbedder
from backend.models.enums import HitlState, ReviewStatus, Severity
from backend.models.review import ReviewRequest
from backend.models.webhook import WebhookEvent
from backend.observability.events import read_trace
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.nodes import NodeDependencies
from backend.settings import get_settings
from backend.tools.llm_client import FakeLLMClient
from scripts.migrate import migrate

pytestmark = pytest.mark.integration


@pytest.fixture
async def engine_setup(db_pool):  # type: ignore[no-untyped-def]
    settings = get_settings()
    await migrate(settings.database_url)
    pool = await create_pool(settings.database_url)

    # Ground the repository, so retrieval is exercised rather than stubbed to empty.
    memory = CodeMemory(pool, FakeEmbedder(settings.embedding_dim), settings)
    await memory.clear_repo(DEMO_REPO)
    await memory.upsert_chunks(
        DEMO_REPO,
        [
            ChunkInput(
                path="billing/stripe.py",
                chunk_index=0,
                symbol="charge_customer",
                content=(
                    "def charge_customer(customer_id, amount):\n"
                    "    return payment_gateway.charge(customer_id, amount)"
                ),
            ),
            ChunkInput(
                path="billing/formatting.py",
                chunk_index=0,
                symbol="money",
                content="def money(cents):\n    return f'${cents / 100:.2f}'",
            ),
        ],
    )

    costs = CostRepository(pool, settings)
    await costs.detect_capabilities()
    agents = build_specialists(
        settings=settings,
        llm=FakeLLMClient(settings),
        memory=memory,
        budget=BudgetGuard(costs, settings),
    )
    deps = NodeDependencies(
        github=DryRunGitHubClient(settings), agents=agents, memory=memory, settings=settings
    )
    engine = LangGraphWorkflowEngine(deps, settings=settings, checkpointer=InMemorySaver())
    await engine.setup()
    yield engine, memory
    await memory.clear_repo(DEMO_REPO)


async def make_review() -> str:
    event = WebhookEvent(
        delivery_id=f"d-{uuid.uuid4()}",
        event_type="pull_request",
        action="opened",
        repo=DEMO_REPO,
        pr_number=DEMO_PR_NUMBER,
        head_sha=f"{DEMO_HEAD_SHA[:-4]}{uuid.uuid4().hex[:4]}",
        title="Add refunds",
        author="dev",
    )
    async with session_scope() as session:
        review = (await ReviewRepository(session).upsert_review_from_event(event)).review
    return str(review.id)


async def test_the_whole_loop_from_review_to_posted_review(engine_setup) -> None:  # type: ignore[no-untyped-def]
    engine, _memory = engine_setup
    settings = get_settings()
    review_id = await make_review()

    async with session_scope() as session:
        review = await ReviewRepository(session).get(review_id)
    assert review is not None

    outcome = await engine.run(
        ReviewRequest(
            review_id=review_id,
            repo=DEMO_REPO,
            pr_number=DEMO_PR_NUMBER,
            head_sha=review.head_sha,
        )
    )

    # 1. it stops and waits for a person
    assert outcome.status is ReviewStatus.PENDING_APPROVAL
    assert outcome.finding_count >= 4
    assert outcome.severity_counts.get(Severity.CRITICAL.value, 0) >= 1

    # 2. findings are persisted, not just returned
    async with session_scope() as session:
        stored = await ReviewRepository(session).list_findings(review_id)
        hitl = await HitlRepository(session).get_for_review(review_id)
    assert len(stored) == outcome.finding_count
    assert hitl is not None and hitl.state == HitlState.AWAITING.value

    # 3. the spine recorded the fan-out with cost attributed
    events = await read_trace(review_id)
    agents_with_llm_calls = {
        str(e.agent) for e in events if str(e.event_type) == "llm.call" and e.cost_usd
    }
    assert {"security", "quality", "tests", "docs"} <= agents_with_llm_calls
    assert any(str(e.event_type) == "escalation" for e in events)

    # 4. retrieval actually grounded the passes
    grounded = [e for e in events if (e.payload or {}).get("retrieved_chunks")]
    assert grounded, "the specialists should report how much context they were given"

    # 5. a human approves, and it publishes through the dry-run client
    service = HitlService(github=DryRunGitHubClient(settings), engine=engine, settings=settings)
    decision = await service.approve(review_id, actor="reviewer@acme.test", note="looks right")

    assert decision.status is ReviewStatus.POSTED
    assert decision.dry_run is True
    assert decision.comment_count >= 1

    outbox = Path(settings.outbox_dir)
    written = await asyncio.to_thread(lambda: sorted(outbox.glob(f"*_{DEMO_PR_NUMBER}_*.json")))
    assert written, "the approved review should have been written to the outbox"
    payload = json.loads(await asyncio.to_thread(written[0].read_text, encoding="utf-8"))
    assert payload["event"] == "COMMENT", "the agent must never approve or block a PR"
    assert payload["comments"], "at least one finding should anchor inline"
    assert "reviewer@acme.test" in payload["body"]

    # 6. the decision itself is on the spine
    trace = await read_trace(review_id)
    posted = [
        e
        for e in trace
        if str(e.event_type) == "tool.call" and (e.payload or {}).get("tool") == "post_review"
    ]
    assert posted, "publication must be recorded on the same spine as everything else"


async def test_a_rejected_review_records_per_finding_feedback(engine_setup) -> None:  # type: ignore[no-untyped-def]
    engine, _memory = engine_setup
    settings = get_settings()
    review_id = await make_review()

    async with session_scope() as session:
        review = await ReviewRepository(session).get(review_id)
    assert review is not None
    await engine.run(
        ReviewRequest(
            review_id=review_id, repo=DEMO_REPO, pr_number=DEMO_PR_NUMBER, head_sha=review.head_sha
        )
    )

    service = HitlService(github=DryRunGitHubClient(settings), engine=engine, settings=settings)
    decision = await service.reject(review_id, actor="reviewer", reason="too noisy for this PR")

    assert decision.status is ReviewStatus.REJECTED

    # Per-finding rows are what make a per-agent rejection rate computable later.
    from backend.hitl.feedback import feedback_signals

    signals = await feedback_signals(pool=_memory.pool)
    assert signals, "rejections must be attributable to an agent"
    assert sum(s.rejections for s in signals) >= 1


async def test_a_superseded_review_cannot_be_approved(engine_setup) -> None:  # type: ignore[no-untyped-def]
    from backend.core.exceptions import ReviewSuperseded

    engine, _memory = engine_setup
    settings = get_settings()
    review_id = await make_review()

    async with session_scope() as session:
        review = await ReviewRepository(session).get(review_id)
    assert review is not None
    await engine.run(
        ReviewRequest(
            review_id=review_id, repo=DEMO_REPO, pr_number=DEMO_PR_NUMBER, head_sha=review.head_sha
        )
    )

    async with session_scope() as session:
        await ReviewRepository(session).supersede_previous_heads(
            DEMO_REPO, DEMO_PR_NUMBER, "a-newer-commit"
        )

    service = HitlService(github=DryRunGitHubClient(settings), engine=engine, settings=settings)
    with pytest.raises(ReviewSuperseded):
        await service.approve(review_id, actor="reviewer")


async def test_a_budget_blocked_review_still_reaches_a_human(engine_setup) -> None:  # type: ignore[no-untyped-def]
    """Degrading to slower-but-correct: no spend, no fabricated findings, and the review
    still arrives in front of a person instead of disappearing."""
    engine, _memory = engine_setup
    settings = get_settings()
    review_id = await make_review()

    # A stub ledger rather than a real one: the spine is shared across the whole suite,
    # so "the budget is exhausted" has to be stated, not arranged by spending first.
    class _ExhaustedLedger:
        async def daily_cost(self) -> float:
            return 10_000.0

    blocked = settings.model_copy(update={"daily_budget_usd": 1.0})
    guard = BudgetGuard(_ExhaustedLedger(), blocked)  # type: ignore[arg-type]
    for agent in engine.nodes.deps.agents.values():
        agent.budget = guard

    async with session_scope() as session:
        review = await ReviewRepository(session).get(review_id)
    assert review is not None
    outcome = await engine.run(
        ReviewRequest(
            review_id=review_id, repo=DEMO_REPO, pr_number=DEMO_PR_NUMBER, head_sha=review.head_sha
        )
    )

    assert outcome.status is ReviewStatus.PENDING_APPROVAL
    assert outcome.finding_count == 0
    assert any("budget" in error for error in outcome.errors)
