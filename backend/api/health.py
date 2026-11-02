"""Health and readiness.

`/healthz` answers "is the process up". `/readyz` answers "can it actually do its job",
which is a different question and the one that matters: it reports the database, the
queue, which storage lanes are live, and whether the events spine has been silently
failing to write. That last one is the important one -- an audit trail with gaps is a
failure the system would otherwise hide.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.api.deps import get_app_context
from backend.database.pg import check_pool, read_capabilities
from backend.database.session import check_database
from backend.job_queue.queue import queue_health
from backend.models.review import HealthResponse
from backend.observability.events import get_emit_failures

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, object]:
    context = get_app_context(request)
    return {
        "status": "ok",
        "app": context.settings.app_name,
        "github_mode": context.github.name,
        "llm_provider": context.llm.name,
        "hitl_mode": context.settings.hitl_mode,
    }


@router.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request) -> HealthResponse:
    context = get_app_context(request)
    database = await check_database()
    pool = await check_pool()
    queue = await queue_health(context.settings)
    capabilities = await read_capabilities() if pool else {}

    ok = database and pool and queue.get("connected", False)
    return HealthResponse(
        status="ready" if ok else "degraded",
        database="ok" if database else "unreachable",
        redis="ok" if queue.get("connected") else f"unreachable: {queue.get('error', '')}",
        has_timescale=bool(capabilities.get("timescale", False)),
        has_vectorscale=bool(capabilities.get("vectorscale", False)),
    )


@router.get("/api/diagnostics")
async def diagnostics(request: Request) -> dict[str, object]:
    """Everything an operator wants on one screen when something looks wrong."""
    context = get_app_context(request)
    capabilities = await read_capabilities()
    return {
        "context": context.describe(),
        "capabilities": capabilities,
        "event_write_failures": get_emit_failures(),
        "queue": await queue_health(context.settings),
        "budget": (await context.budget.status()).__dict__,
    }
