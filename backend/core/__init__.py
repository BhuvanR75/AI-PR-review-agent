"""Core: the abstract orchestration interface and shared exceptions.

This package depends on nothing else in the application. Outer modules depend
inward only, so any outer module can be deleted and the inner ones still compile.
"""

from backend.core.exceptions import (
    GitHubError,
    InvalidReviewState,
    PullRequestUnavailable,
    ReviewAgentError,
    ReviewNotFound,
    ReviewSuperseded,
    WorkflowError,
)
from backend.core.workflow_engine import WorkflowEngine

__all__ = [
    "GitHubError",
    "InvalidReviewState",
    "PullRequestUnavailable",
    "ReviewAgentError",
    "ReviewNotFound",
    "ReviewSuperseded",
    "WorkflowEngine",
    "WorkflowError",
]
