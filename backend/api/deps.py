"""FastAPI dependencies. One place where the app context is fetched."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from backend.bootstrap import AppContext
from backend.hitl.decisions import HitlService


def get_app_context(request: Request) -> AppContext:
    context = getattr(request.app.state, "context", None)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application context is not initialised",
        )
    return context


def get_hitl_service(request: Request) -> HitlService:
    service = getattr(request.app.state, "hitl", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="approval service is not initialised",
        )
    return service
