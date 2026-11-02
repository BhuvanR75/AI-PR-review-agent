"""HTTP surface: ingress, reviews, approvals, economics, metadata."""

from backend.api.economics_router import router as economics_router
from backend.api.github_router import router as github_router
from backend.api.health import router as health_router
from backend.api.hitl_router import router as hitl_router
from backend.api.meta import router as meta_router
from backend.api.queue import router as queue_router
from backend.api.reviews import router as reviews_router

__all__ = [
    "economics_router",
    "github_router",
    "health_router",
    "hitl_router",
    "meta_router",
    "queue_router",
    "reviews_router",
]
