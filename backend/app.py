"""The FastAPI application.

Two routers are wired differently on purpose: the webhook router is unauthenticated
(GitHub proves itself with an HMAC over the body, which is a stronger and more
appropriate check than a bearer token), while every other route requires the bearer
token. Read routes are protected too -- a review contains repository content, so "it is
only a read" is not an argument.

The application context is built once in the lifespan and stored on `app.state`, so a
request never constructs a client, a pool or a circuit breaker.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api import (
    economics_router,
    github_router,
    health_router,
    hitl_router,
    meta_router,
    queue_router,
    reviews_router,
)
from backend.api.auth import require_token
from backend.bootstrap import AppContext, build_context, shutdown_context
from backend.observability.logging import configure_logging
from backend.settings import Settings, get_settings
from backend.webhook_receiver.router import router as webhook_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    resolved = settings or get_settings()

    app = FastAPI(
        title="AI PR Review Agent",
        version="0.1.0",
        description=(
            "A fan-out of specialist reasoners over a pull request diff, grounded in "
            "retrieved codebase context, with every action written to one time-ordered "
            "spine and every review gated by a human before it posts."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Unauthenticated: GitHub proves authenticity with the HMAC, not a token.
    app.include_router(webhook_router)
    app.include_router(health_router)

    protected = [Depends(require_token)]
    for router in (
        reviews_router,
        hitl_router,
        economics_router,
        queue_router,
        meta_router,
        github_router,
    ):
        app.include_router(router, dependencies=protected)

    @app.on_event("startup")
    async def _startup() -> None:
        context: AppContext = await build_context(settings=resolved)
        app.state.context = context
        app.state.hitl = context.hitl
        if not resolved.api_token:
            logger.warning(
                "API_TOKEN is empty: the approval surface is UNAUTHENTICATED. "
                "Only acceptable on a private network."
            )
        logger.info("api ready", extra={"context": context.describe()})

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        from backend.job_queue.queue import close_queue

        await shutdown_context(getattr(app.state, "context", None))
        await close_queue()

    return app


app = create_app()
