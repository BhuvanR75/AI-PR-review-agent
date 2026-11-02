"""Parse a GitHub `pull_request` payload into the one shape the graph consumes.

Everything the graph needs and nothing it does not. Two guards matter:

  * A payload whose shape we do not recognise raises `PayloadError` rather than
    producing a half-populated event. Enqueuing a review with an empty head_sha would
    later fail inside the workflow, far from the cause.
  * `is_reviewable` is checked before enqueuing. GitHub sends pull_request webhooks for
    `labeled`, `assigned`, `edited` and more; reviewing on those would multiply cost
    for no benefit and make the agent look noisy.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.models.review import ChangedFile
from backend.models.webhook import REVIEWABLE_ACTIONS, WebhookEvent

logger = logging.getLogger(__name__)


class PayloadError(Exception):
    """The payload is not a pull_request event we can act on."""


def parse_pull_request_event(payload: dict[str, Any], *, delivery_id: str) -> WebhookEvent:
    action = payload.get("action") or ""
    pull_request = payload.get("pull_request")
    repository = payload.get("repository")

    if not isinstance(pull_request, dict) or not isinstance(repository, dict):
        raise PayloadError("payload is missing pull_request or repository")

    head = pull_request.get("head") or {}
    base = pull_request.get("base") or {}
    head_sha = head.get("sha")
    if not head_sha:
        raise PayloadError("pull_request.head.sha is missing")

    full_name = repository.get("full_name")
    number = pull_request.get("number")
    if not full_name or number is None:
        raise PayloadError("repository.full_name or pull_request.number is missing")

    installation = payload.get("installation") or {}

    return WebhookEvent(
        delivery_id=delivery_id,
        event_type="pull_request",
        action=action,
        repo=full_name,
        pr_number=int(number),
        head_sha=head_sha,
        base_sha=base.get("sha"),
        title=pull_request.get("title"),
        author=(pull_request.get("user") or {}).get("login"),
        is_draft=bool(pull_request.get("draft", False)),
        installation_id=installation.get("id"),
    )


def summarize(event: WebhookEvent) -> str:
    return (
        f"{event.repo}#{event.pr_number} {event.action} "
        f"@{event.head_sha[:8]} (reviewable={event.is_reviewable})"
    )


def skip_reason(event: WebhookEvent) -> str | None:
    if event.action not in REVIEWABLE_ACTIONS:
        return f"action '{event.action}' does not warrant a review"
    if event.is_draft:
        return "pull request is a draft"
    return None


__all__ = ["PayloadError", "ChangedFile", "parse_pull_request_event", "skip_reason", "summarize"]
