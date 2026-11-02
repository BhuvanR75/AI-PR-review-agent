"""The reject decision now has a GitHub side effect: the reason is posted as a comment
and the pull request is closed.

What these tests pin down:

  * the reason actually reaches the GitHub client (comment first, close second);
  * GitHub-first ordering -- a token that cannot comment aborts the rejection before
    any local state changes, so the review stays pending and retryable;
  * the dry-run client writes the rejection to the outbox, mirroring production
    behaviour offline.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import backend.hitl.decisions as decisions_module
from backend.core.exceptions import GitHubError
from backend.hitl.decisions import HitlService
from backend.integrations.github_models import ClosedPullRequest
from backend.models.enums import ReviewStatus
from backend.settings import Settings

REVIEW_ID = "11111111-2222-3333-4444-555555555555"


class _Session:
    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def close(self) -> None: ...


@asynccontextmanager
async def _fake_session_scope():  # type: ignore[no-untyped-def]
    yield _Session()


class FakeReviewRepository:
    last_status: Any = None

    def __init__(self, session: Any) -> None: ...

    async def get(self, review_id: str) -> Any:
        return SimpleNamespace(
            id=review_id,
            repo="acme/shop",
            pr_number=42,
            head_sha="abc123",
            status=ReviewStatus.PENDING_APPROVAL.value,
            overall_confidence=0.7,
            delivery_id=None,
        )

    async def list_findings(self, review_id: str) -> list[Any]:
        return []

    async def set_status(self, review_id: str, status: Any, **kwargs: Any) -> None:
        FakeReviewRepository.last_status = status


class FakeHitlRepository:
    decided: list[tuple[str, str]] = []
    feedback: list[str] = []

    def __init__(self, session: Any) -> None: ...

    async def get_for_review(self, review_id: str) -> None:
        return None

    async def decide(self, review_id: str, state: Any, *, actor: str, notes: str | None) -> None:
        FakeHitlRepository.decided.append((review_id, str(state)))

    async def add_feedback(
        self,
        review_id: str,
        *,
        actor: str,
        kind: str,
        comment: str,
        finding_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        FakeHitlRepository.feedback.append(comment)


class RecordingGitHub:
    name = "recording"
    dry_run = True

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_pull_request(self, repo: str, number: int) -> Any: ...  # pragma: no cover

    async def list_repos(self) -> list[Any]: ...  # pragma: no cover

    async def list_pulls(self, repo: str) -> list[Any]: ...  # pragma: no cover

    async def validate(self) -> Any: ...  # pragma: no cover

    async def post_review(self, repo: str, number: int, **kwargs: Any) -> Any:
        ...  # pragma: no cover

    async def close_pull_request(
        self, repo: str, number: int, *, reason: str, actor: str, review_id: str
    ) -> ClosedPullRequest:
        self.calls.append(
            {
                "repo": repo,
                "number": number,
                "reason": reason,
                "actor": actor,
                "review_id": review_id,
            }
        )
        return ClosedPullRequest(
            commented=True, closed=True, dry_run=True, summary="dry run: recorded rejection"
        )


class RefusingGitHub(RecordingGitHub):
    """A token that cannot comment: the rejection must abort before local writes."""

    async def close_pull_request(
        self, repo: str, number: int, *, reason: str, actor: str, review_id: str
    ) -> ClosedPullRequest:
        raise GitHubError("GitHub refused the post: 403 not accessible", 403)


@pytest.fixture()
def stubbed_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(decisions_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(decisions_module, "ReviewRepository", FakeReviewRepository)
    monkeypatch.setattr(decisions_module, "HitlRepository", FakeHitlRepository)
    monkeypatch.setattr(decisions_module, "get_pool", lambda: object())
    monkeypatch.setattr(
        decisions_module, "CostRepository", lambda pool, settings: SimpleNamespace()
    )
    FakeHitlRepository.decided = []
    FakeHitlRepository.feedback = []
    FakeReviewRepository.last_status = None


async def test_reject_posts_reason_and_closes_the_pr(stubbed_persistence: None) -> None:
    github = RecordingGitHub()
    service = HitlService(github=github, settings=Settings())

    outcome = await service.reject(REVIEW_ID, actor="reviewer@local", reason="duplicates #17")

    assert [c["reason"] for c in github.calls] == ["duplicates #17"]
    assert github.calls[0]["repo"] == "acme/shop"
    assert github.calls[0]["number"] == 42
    assert outcome.status is ReviewStatus.REJECTED
    assert outcome.extra["pr_closed"] is True
    assert FakeReviewRepository.last_status is ReviewStatus.REJECTED
    assert FakeHitlRepository.decided == [(REVIEW_ID, "rejected")]


async def test_reject_aborts_before_local_writes_when_github_refuses(
    stubbed_persistence: None,
) -> None:
    service = HitlService(github=RefusingGitHub(), settings=Settings())

    with pytest.raises(GitHubError):
        await service.reject(REVIEW_ID, actor="reviewer@local", reason="duplicates #17")

    # The review must stay pending: no decision, no feedback, no status change.
    assert FakeHitlRepository.decided == []
    assert FakeHitlRepository.feedback == []
    assert FakeReviewRepository.last_status is None


async def test_dry_run_reject_writes_the_outbox(
    stubbed_persistence: None, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.integrations.github_client import DryRunGitHubClient

    outbox = tmp_path / "outbox"
    settings = Settings(outbox_dir=str(outbox))
    github = DryRunGitHubClient(settings)
    service = HitlService(github=github, settings=settings)

    outcome = await service.reject(REVIEW_ID, actor="reviewer@local", reason="duplicates #17")

    written = list((tmp_path / "outbox").glob("acme__shop_42_closed.json"))
    assert written, "the dry-run rejection must land in the outbox"
    payload = json.loads(written[0].read_text(encoding="utf-8"))
    assert payload["action"] == "close"
    assert "duplicates #17" in payload["comment"]
    assert outcome.dry_run is True
