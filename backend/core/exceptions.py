"""Shared exception types.

These exist so the layers above can distinguish causes without matching on message
strings: `ReviewSuperseded` in particular must be handled differently from a failure,
because a superseded review is a correct outcome, not an error.
"""

from __future__ import annotations


class ReviewAgentError(Exception):
    """Base class for everything this application raises deliberately."""


class ReviewNotFound(ReviewAgentError):
    def __init__(self, review_id: str) -> None:
        super().__init__(f"review '{review_id}' not found")
        self.review_id = review_id


class InvalidReviewState(ReviewAgentError):
    """The requested transition is not legal for the review's current status."""


class ReviewSuperseded(ReviewAgentError):
    """A newer commit exists for this pull request, so this review must not post."""

    def __init__(self, review_id: str) -> None:
        super().__init__(f"review '{review_id}' was superseded by a newer commit")
        self.review_id = review_id


class WorkflowError(ReviewAgentError):
    """The orchestration engine could not complete or resume the workflow."""


class GitHubError(ReviewAgentError):
    """A GitHub API call failed after retries."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class PullRequestUnavailable(GitHubError):
    """The PR could not be read -- deleted, private, or the token lacks access."""
