"""Webhook payload contract: the precise trigger, reduced to what the graph needs."""

from __future__ import annotations

from pydantic import BaseModel, Field

from backend.models.review import ChangedFile

#: Only these PR actions are worth a review. A webhook for `labeled` or `review_requested`
#: should be acknowledged and dropped, not enqueued.
REVIEWABLE_ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})


class WebhookEvent(BaseModel):
    delivery_id: str = Field(min_length=1, description="X-GitHub-Delivery. The idempotency key.")
    event_type: str
    action: str
    repo: str = Field(description="owner/name")
    pr_number: int
    head_sha: str
    base_sha: str | None = None
    title: str | None = None
    author: str | None = None
    is_draft: bool = False
    installation_id: int | None = None
    changed_files: list[ChangedFile] = Field(default_factory=list)

    @property
    def is_reviewable(self) -> bool:
        return self.action in REVIEWABLE_ACTIONS and not self.is_draft

    @property
    def supersede_key(self) -> str:
        """One review per (repo, pr, head_sha). A new push supersedes the old review."""
        return f"{self.repo}#{self.pr_number}@{self.head_sha}"
