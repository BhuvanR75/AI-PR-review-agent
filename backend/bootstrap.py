"""Bootstrap: the one place where the object graph is assembled.

The API process, the ARQ worker and the CLI all build exactly this context, so there is
one answer to "which client is live right now" rather than three that can disagree. In
particular the LLM client is created once, which means one circuit breaker per provider
instead of one per caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.agents import build_specialists
from backend.core.workflow_engine import WorkflowEngine
from backend.database.pg import close_pool, create_pool
from backend.database.session import dispose_engine
from backend.economics.budget import BudgetGuard
from backend.economics.cost_repository import CostRepository
from backend.hitl.decisions import HitlService
from backend.integrations.connection_store import ConnectionStore
from backend.integrations.github_client import (
    GitHubClient,
    ResolvingGitHubClient,
)
from backend.memory.code_memory import CodeMemory
from backend.memory.embedder import Embedder, get_embedder
from backend.observability.logging import configure_logging
from backend.orchestrator.langgraph_engine import LangGraphWorkflowEngine
from backend.orchestrator.nodes import NodeDependencies
from backend.settings import Settings, get_settings
from backend.tools.llm_client import LLMClient, get_llm_client

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    github: GitHubClient
    llm: LLMClient
    embedder: Embedder
    engine: WorkflowEngine
    costs: CostRepository
    budget: BudgetGuard
    hitl: HitlService
    memory: CodeMemory | None = None
    engine_name: str = "langgraph"
    checkpointer: str = "uninitialised"
    extras: dict[str, object] = field(default_factory=dict)

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self.github, "dry_run", True))

    def describe(self) -> dict[str, object]:
        return {
            "llm_provider": self.llm.name,
            "embedding_provider": self.embedder.name,
            "github": self.github.name,
            "github_dry_run": self.dry_run,
            "engine": self.engine_name,
            "checkpointer": getattr(self.engine, "checkpoint_backend", "unknown"),
            "hitl_mode": self.settings.hitl_mode,
            "rolling_rollups": self.costs.use_rollups,
        }


async def build_context(
    *,
    settings: Settings | None = None,
    with_engine: bool = True,
    agents: list[str] | None = None,
) -> AppContext:
    configure_logging()
    resolved = settings or get_settings()

    pool = await create_pool(resolved.database_url)

    llm = get_llm_client(resolved)
    embedder = get_embedder(resolved)
    github = ResolvingGitHubClient(ConnectionStore(resolved.database_url), resolved)
    costs = CostRepository(pool, resolved)
    await costs.detect_capabilities()
    budget = BudgetGuard(costs, resolved)
    hitl = HitlService(github=github, settings=resolved)

    memory = CodeMemory(pool, embedder, resolved)

    specialists = build_specialists(
        llm=llm,
        memory=memory,
        budget=budget,
        settings=resolved,
    )

    deps = NodeDependencies(github=github, agents=specialists, memory=memory, settings=resolved)
    engine: WorkflowEngine = LangGraphWorkflowEngine(deps, settings=resolved, agents=agents)
    if with_engine:
        await engine.setup()

    # The HITL service needs the engine for re-runs, and the engine is built after it is
    # needed, so the reference is attached once both exist.
    hitl.engine = engine

    context = AppContext(
        settings=resolved,
        github=github,
        llm=llm,
        embedder=embedder,
        engine=engine,
        costs=costs,
        budget=budget,
        hitl=hitl,
        memory=memory,
        engine_name=getattr(engine, "name", "unknown"),
        checkpointer=getattr(engine, "checkpoint_backend", "unknown"),
    )
    logger.info("application context ready", extra={"context": context.describe()})
    return context


async def shutdown_context(context: AppContext | None = None) -> None:
    if context is not None and context.engine is not None:
        try:
            await context.engine.teardown()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("engine teardown failed")
    await close_pool()
    await dispose_engine()
