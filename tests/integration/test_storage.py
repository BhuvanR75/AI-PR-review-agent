"""Storage-lane integration tests.

These exercise the parts that cannot be tested with a fake pool: the pgvector parameter
cast, the tsvector generated column, the conditional insert used for idempotency, and
the transactional behaviours (superseding, replacing findings on re-run).
"""

from __future__ import annotations

import uuid

import pytest

from backend.database.repository import HitlRepository, ReviewRepository
from backend.database.session import session_scope
from backend.memory.code_memory import ChunkInput, CodeMemory
from backend.memory.embedder import FakeEmbedder
from backend.models.enums import AgentType, Category, HitlState, ReviewStatus, Severity
from backend.models.findings import Finding
from backend.models.webhook import WebhookEvent
from backend.observability.events import emit_agent_event, read_trace
from backend.reliability.idempotency import get_delivery, mark_delivery, reserve_delivery
from backend.settings import get_settings
from scripts.migrate import migrate

pytestmark = pytest.mark.integration

REPO = "acme/shop"


@pytest.fixture(autouse=True)
async def _schema(db_pool) -> None:  # type: ignore[no-untyped-def]
    await migrate(get_settings().database_url)


@pytest.fixture
def memory(db_pool) -> CodeMemory:  # type: ignore[no-untyped-def]
    settings = get_settings()
    return CodeMemory(db_pool, FakeEmbedder(settings.embedding_dim), settings)


async def test_chunks_upsert_and_replace_on_reindex(memory: CodeMemory) -> None:
    await memory.clear_repo(REPO)
    first = [
        ChunkInput(path="billing/stripe.py", chunk_index=0, content="def charge_customer(): pass"),
        ChunkInput(path="billing/stripe.py", chunk_index=1, content="def refund_customer(): pass"),
    ]
    assert await memory.upsert_chunks(REPO, first) == 2
    assert (await memory.stats(REPO)).chunks == 2

    # Re-index with one fewer chunk: the orphan must be removed, not left stale.
    await memory.delete_file_chunks(REPO, "billing/stripe.py", keep=1)
    stats = await memory.stats(REPO)
    assert stats.chunks == 1


async def test_upsert_is_idempotent_on_the_same_content(memory: CodeMemory) -> None:
    await memory.clear_repo(REPO)
    chunk = ChunkInput(path="a.py", chunk_index=0, content="x = 1")
    await memory.upsert_chunks(REPO, [chunk])
    await memory.upsert_chunks(REPO, [chunk])
    assert (await memory.stats(REPO)).chunks == 1


async def test_vector_lane_finds_the_semantically_closest_chunk(memory: CodeMemory) -> None:
    """The float4[]-to-vector cast is the reason this module uses raw asyncpg; if the
    cast were wrong this test fails rather than the query silently returning noise."""
    await memory.clear_repo(REPO)
    await memory.upsert_chunks(
        REPO,
        [
            ChunkInput(
                path="billing/stripe.py",
                chunk_index=0,
                symbol="charge_customer",
                content="def charge_customer(customer_id, amount):\n    return gateway.charge(...)",
            ),
            ChunkInput(
                path="ui/render.py",
                chunk_index=0,
                symbol="render_invoice",
                content="def render_invoice(document):\n    return template.render(document)",
            ),
        ],
    )
    hits = await memory.search_vector(REPO, "charge_customer amount", limit=2)
    assert hits
    assert hits[0].path == "billing/stripe.py"


async def test_keyword_lane_matches_exact_identifiers(memory: CodeMemory) -> None:
    await memory.clear_repo(REPO)
    await memory.upsert_chunks(
        REPO,
        [
            ChunkInput(path="ops/config.py", chunk_index=0, content="STRIPE_WEBHOOK_SECRET = 'x'"),
            ChunkInput(path="billing/pay.py", chunk_index=0, content="def pay(): return 1"),
        ],
    )
    hits = await memory.search_keyword(REPO, "STRIPE_WEBHOOK_SECRET", limit=5)
    assert hits
    assert hits[0].path == "ops/config.py"


async def test_hybrid_search_returns_top_k(memory: CodeMemory) -> None:
    await memory.clear_repo(REPO)
    await memory.upsert_chunks(
        REPO,
        [
            ChunkInput(path=f"mod_{i}.py", chunk_index=0, content=f"def handler_{i}(): pass")
            for i in range(20)
        ],
    )
    hits = await memory.search(REPO, "handler_3", k=5)
    assert len(hits) == 5


async def test_freshness_tracking_round_trips(memory: CodeMemory) -> None:
    await memory.clear_repo(REPO)
    from backend.memory.code_memory import IndexedFile

    await memory.record_files(REPO, [IndexedFile(path="a.py", content_hash="h1", size_bytes=10)])
    indexed = await memory.indexed_files(REPO)
    assert indexed == {"a.py": "h1"}


# --- events spine -----------------------------------------------------------
async def test_events_round_trip_and_trace_orders_by_time(db_pool, review_id: str) -> None:  # type: ignore[no-untyped-def]
    await emit_agent_event(
        agent="security",
        event_type="span.start",
        review_id=review_id,
        pool=db_pool,
    )
    await emit_agent_event(
        agent="security",
        event_type="llm.call",
        review_id=review_id,
        model="gpt-4o-mini",
        tokens_in=10,
        tokens_out=4,
        cost_usd=0.0002,
        latency_ms=1200,
        confidence=0.8,
        payload={"prompt_hash": "abc", "retrieved_chunks": 3},
        pool=db_pool,
    )
    await emit_agent_event(
        agent="security", event_type="span.end", review_id=review_id, outcome="ok", pool=db_pool
    )

    events = await read_trace(review_id, pool=db_pool)
    assert [str(e.event_type) for e in events] == ["span.start", "llm.call", "span.end"]
    assert events[1].payload["prompt_hash"] == "abc"
    assert events[1].cost_usd == pytest.approx(0.0002)


async def test_cost_ledger_totals_match_the_raw_rows(db_pool, review_id: str) -> None:  # type: ignore[no-untyped-def]
    from backend.economics.cost_repository import CostRepository

    for cost in (0.001, 0.002):
        await emit_agent_event(
            agent="quality",
            event_type="llm.call",
            review_id=review_id,
            cost_usd=cost,
            tokens_in=100,
            tokens_out=50,
            pool=db_pool,
        )

    costs = CostRepository(db_pool, get_settings())
    ledger = await costs.review_cost(review_id)
    assert ledger.usd == pytest.approx(0.003)
    assert ledger.llm_calls == 2
    assert ledger.by_agent["quality"] == pytest.approx(0.003)
    assert ledger.tokens_in == 200


async def test_budget_reads_the_same_spine(db_pool) -> None:  # type: ignore[no-untyped-def]
    from backend.economics.budget import BudgetGuard
    from backend.economics.cost_repository import CostRepository

    guard = BudgetGuard(CostRepository(db_pool, get_settings()), get_settings())
    status = await guard.status()
    assert status.allowed  # tests run with a fresh budget
    assert status.spent_today >= 0


# --- idempotency ------------------------------------------------------------
async def test_a_delivery_can_only_be_reserved_once(db_pool) -> None:  # type: ignore[no-untyped-def]
    delivery = f"delivery-{uuid.uuid4()}"
    assert await reserve_delivery(
        delivery_id=delivery, event_type="pull_request", repo=REPO, pr_number=1, pool=db_pool
    )
    assert not await reserve_delivery(
        delivery_id=delivery, event_type="pull_request", repo=REPO, pr_number=1, pool=db_pool
    )


async def test_delivery_status_is_recorded(db_pool) -> None:  # type: ignore[no-untyped-def]
    delivery = f"delivery-{uuid.uuid4()}"
    await reserve_delivery(delivery_id=delivery, event_type="pull_request", pool=db_pool)
    await mark_delivery(delivery, "done", pool=db_pool)
    row = await get_delivery(delivery, pool=db_pool)
    assert row is not None
    assert row["status"] == "done"


# --- review repository ------------------------------------------------------
def event_for(head: str = "sha-one") -> WebhookEvent:
    return WebhookEvent(
        delivery_id=f"d-{uuid.uuid4()}",
        event_type="pull_request",
        action="opened",
        repo=REPO,
        pr_number=99,
        head_sha=head,
        title="Change",
        author="dev",
    )


async def test_review_upsert_is_idempotent_per_commit() -> None:
    event = event_for(head=f"sha-{uuid.uuid4()}")
    async with session_scope() as session:
        first = await ReviewRepository(session).upsert_review_from_event(event)
    async with session_scope() as session:
        second = await ReviewRepository(session).upsert_review_from_event(event)

    assert first.created is True
    assert second.created is False
    assert str(first.review.id) == str(second.review.id)
    assert second.redrivable is True


async def test_a_new_commit_supersedes_the_previous_head() -> None:
    """The guarantee that stops a human approving a review of code that no longer
    exists."""
    pr_number = uuid.uuid4().int % 100000
    old = event_for(head=f"old-{uuid.uuid4()}")
    old.pr_number = pr_number
    new = event_for(head=f"new-{uuid.uuid4()}")
    new.pr_number = pr_number

    async with session_scope() as session:
        reviews = ReviewRepository(session)
        old_review = (await reviews.upsert_review_from_event(old)).review
        await reviews.set_status(str(old_review.id), ReviewStatus.PENDING_APPROVAL)
        new_review = (await reviews.upsert_review_from_event(new)).review
        superseded = await reviews.supersede_previous_heads(REPO, pr_number, new.head_sha)
        await reviews.set_status(str(new_review.id), ReviewStatus.RUNNING)

    assert str(old_review.id) in superseded

    async with session_scope() as session:
        refreshed = await ReviewRepository(session).get(str(old_review.id))
    assert refreshed is not None
    assert refreshed.status == ReviewStatus.SUPERSEDED.value


async def test_a_superseded_review_leaves_the_approval_queue() -> None:
    pr_number = uuid.uuid4().int % 100000
    old = event_for(head=f"old-{uuid.uuid4()}")
    old.pr_number = pr_number
    new = event_for(head=f"new-{uuid.uuid4()}")
    new.pr_number = pr_number

    async with session_scope() as session:
        reviews = ReviewRepository(session)
        hitl = HitlRepository(session)
        old_review = (await reviews.upsert_review_from_event(old)).review
        await reviews.set_status(str(old_review.id), ReviewStatus.PENDING_APPROVAL)
        await hitl.enqueue(str(old_review.id), "awaiting")
        await reviews.supersede_previous_heads(REPO, pr_number, new.head_sha)

    async with session_scope() as session:
        record = await HitlRepository(session).get_for_review(str(old_review.id))
    assert record is not None
    assert record.state == HitlState.SUPERSEDED.value


async def test_findings_replace_rather_than_accumulate_on_rerun() -> None:
    event = event_for(head=f"sha-{uuid.uuid4()}")
    async with session_scope() as session:
        reviews = ReviewRepository(session)
        review = (await reviews.upsert_review_from_event(event)).review
        marker = Finding(
            agent_type=AgentType.SECURITY,
            severity=Severity.CRITICAL,
            category=Category.INJECTION,
            summary="first pass",
            file_path="a.py",
            line_start=1,
            confidence=0.9,
            rationale="first",
        )
        await reviews.store_findings(str(review.id), [marker])
        assert len(await reviews.list_findings(str(review.id))) == 1

        removed = await reviews.delete_findings(str(review.id))
        assert removed == 1
        assert await reviews.list_findings(str(review.id)) == []


async def test_findings_round_trip_with_all_their_fields() -> None:
    event = event_for(head=f"sha-{uuid.uuid4()}")
    original = Finding(
        agent_type=AgentType.QUALITY,
        severity=Severity.MEDIUM,
        category=Category.CORRECTNESS,
        summary="mutable default argument",
        file_path="billing/stripe.py",
        line_start=12,
        line_end=12,
        suggestion="default to None",
        confidence=0.84,
        rationale="the list is shared across calls",
        agreed_by=[AgentType.SECURITY],
        incomplete=False,
    )
    async with session_scope() as session:
        reviews = ReviewRepository(session)
        review = (await reviews.upsert_review_from_event(event)).review
        await reviews.store_findings(str(review.id), [original])

    async with session_scope() as session:
        stored = await ReviewRepository(session).list_findings(str(review.id))
    assert len(stored) == 1
    assert stored[0].finding == original
    assert stored[0].id


async def test_dedup_near_misses_are_persisted_for_later_tuning() -> None:
    event = event_for(head=f"sha-{uuid.uuid4()}")
    async with session_scope() as session:
        reviews = ReviewRepository(session)
        review = (await reviews.upsert_review_from_event(event)).review
        await reviews.record_near_misses(
            str(review.id),
            [
                {
                    "kept": "a.py:10:correctness",
                    "dropped": "a.py:12:correctness",
                    "agent": "quality",
                    "reason": "within 5 lines but a different anchor",
                }
            ],
        )
