"""The LangGraph implementation of `core.workflow_engine.WorkflowEngine`.

Checkpointing: Redis when the Redis Stack modules are reachable, in memory otherwise.
The fallback is a real limitation rather than a silent one -- with the in-memory saver,
`resume` only works within the lifetime of the process, so a worker restart mid-review
loses that review's progress and it must be dispatched again (which the ingress
idempotency key makes safe). It is logged loudly at setup so the state is never
guessed at.

Every node call is wrapped in a timeout at the agent level, so this class bounds
runtime with the graph's recursion limit rather than per-node timeouts of its own.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.core.exceptions import WorkflowError
from backend.models.enums import ReviewStatus
from backend.models.review import ReviewOutcome, ReviewRequest
from backend.observability.tracing import bind_review
from backend.orchestrator.graph import build_graph
from backend.orchestrator.nodes import NodeDependencies, ReviewNodes
from backend.orchestrator.serde import attach_serializer
from backend.orchestrator.state import ReviewState, initial_state
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)


class LangGraphWorkflowEngine:
    name = "langgraph"

    def __init__(
        self,
        deps: NodeDependencies,
        *,
        settings: Settings | None = None,
        agents: list[str] | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.nodes = ReviewNodes(deps)
        self.agents = agents
        # Injecting a checkpointer is a test seam: it lets the graph be exercised in
        # memory deterministically instead of depending on Redis being up.
        self._checkpointer = checkpointer
        self._graph: Any = None
        self.checkpoint_backend: str = "injected" if checkpointer is not None else "uninitialised"

    # --- lifecycle -----------------------------------------------------------
    async def setup(self) -> None:
        if self._graph is not None:
            return
        if self._checkpointer is None:
            self._checkpointer = await self._build_checkpointer()
        self._graph = build_graph(self.nodes, checkpointer=self._checkpointer)
        logger.info(
            "workflow engine ready",
            extra={"context": {"checkpointer": self.checkpoint_backend, "engine": self.name}},
        )

    async def teardown(self) -> None:
        self._graph = None
        self._checkpointer = None

    async def _build_checkpointer(self) -> Any:
        """Redis-backed if the modules are there, in memory if not."""
        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            saver = AsyncRedisSaver(redis_url=self.settings.redis_url)
            await saver.asetup()
            self.checkpoint_backend = "redis"
            return attach_serializer(saver)
        except Exception as exc:  # noqa: BLE001 - degrade, loudly
            logger.warning(
                "Redis checkpointer unavailable (%s); falling back to in-memory "
                "checkpointing, so resume will not survive a process restart",
                exc,
            )
            from langgraph.checkpoint.memory import InMemorySaver

            self.checkpoint_backend = "memory"
            return attach_serializer(InMemorySaver())

    @property
    def graph(self) -> Any:
        if self._graph is None:
            raise WorkflowError("engine.setup() must be awaited before use")
        return self._graph

    # --- execution -----------------------------------------------------------
    def _config(self, thread_id: str) -> dict[str, Any]:
        return {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self.settings.recursion_limit,
        }

    async def run(self, request: ReviewRequest, *, thread_id: str | None = None) -> ReviewOutcome:
        await self.setup()
        resolved_thread = thread_id or request.review_id
        state = initial_state(request, agents=self.agents)
        with bind_review(request.review_id):
            try:
                final: ReviewState = await self.graph.ainvoke(
                    state, config=self._config(resolved_thread)
                )
            except Exception as exc:
                raise WorkflowError(f"review workflow failed: {exc}") from exc
        return self._outcome(final, request.review_id)

    async def resume(self, request: ReviewRequest) -> ReviewOutcome:
        """Continue from the last checkpoint. Passing `None` as input tells LangGraph
        to pick up from the persisted state instead of starting fresh."""
        await self.setup()
        with bind_review(request.review_id):
            try:
                final: ReviewState = await self.graph.ainvoke(
                    None, config=self._config(request.review_id)
                )
            except Exception as exc:
                raise WorkflowError(f"could not resume review workflow: {exc}") from exc
        return self._outcome(final, request.review_id)

    async def get_state(self, thread_id: str) -> dict[str, Any] | None:
        await self.setup()
        snapshot = await self.graph.aget_state(self._config(thread_id))
        if snapshot is None or not getattr(snapshot, "values", None):
            return None
        return dict(snapshot.values)

    # --- result shaping ------------------------------------------------------
    @staticmethod
    def _outcome(state: ReviewState | dict[str, Any], review_id: str) -> ReviewOutcome:
        values: dict[str, Any] = dict(state)
        decision = values.get("decision")
        if values.get("posted"):
            status = ReviewStatus.POSTED
        elif decision == "superseded":
            status = ReviewStatus.SUPERSEDED
        elif decision in {"human_approval", "escalate"}:
            status = ReviewStatus.PENDING_APPROVAL
        elif decision == "post":
            status = ReviewStatus.POSTED
        else:
            status = ReviewStatus.FAILED

        findings = values.get("findings", []) or []
        return ReviewOutcome(
            review_id=review_id,
            status=status,
            decision=decision,
            overall_confidence=values.get("overall_confidence"),
            finding_count=len(findings),
            severity_counts=values.get("severity_counts", {}) or {},
            errors=list(values.get("errors", []) or []),
            cost_usd=round(
                sum(getattr(r, "cost_usd", 0.0) for r in values.get("results", []) or []), 6
            ),
            critical_findings=[f for f in findings if getattr(f, "severity", None) == "CRITICAL"],
        )
