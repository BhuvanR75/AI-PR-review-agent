"""GitHub access, behind one interface.

`DryRunGitHubClient` is the default. It reads a PR from `.local/inbox` if a fixture
is there, otherwise it returns a built-in demo PR, and it writes any review it is
asked to post into `.local/outbox` instead of calling GitHub. That is what lets the
whole loop -- webhook, queue, fan-out, aggregator, human approval, publication -- run
and be tested without a token, and it is what the console shows when you approve
something before you have configured a GitHub App.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from backend.core.exceptions import GitHubError, PullRequestUnavailable
from backend.integrations.connection_store import ConnectionStore
from backend.integrations.demo_pr import (
    DEMO_AUTHOR,
    DEMO_BASE_SHA,
    DEMO_CHANGED_FILES,
    DEMO_DIFF,
    DEMO_HEAD_SHA,
    DEMO_TITLE,
)
from backend.integrations.github_models import (
    ClosedPullRequest,
    ConnectionCheck,
    PostedReview,
    PullRequestInfo,
    PullRequestSummary,
    RepoSummary,
    ReviewComment,
)
from backend.models.review import ChangedFile
from backend.reliability.circuit_breaker import get_breaker
from backend.reliability.retry import RetryPolicy, call_with_retry
from backend.settings import Settings, get_settings

logger = logging.getLogger(__name__)

REJECTION_HEADER = (
    "## Review rejected\n\nA human reviewer rejected this pull request after an "
    "automated review. Reason:\n\n> "
)


class GitHubClient(Protocol):
    name: str
    dry_run: bool

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo: ...

    async def list_repos(self) -> list[RepoSummary]: ...

    async def list_pulls(self, repo: str) -> list[PullRequestSummary]: ...

    async def validate(self) -> ConnectionCheck: ...

    async def post_review(
        self,
        repo: str,
        number: int,
        *,
        head_sha: str,
        body: str,
        comments: list[ReviewComment],
    ) -> PostedReview: ...

    async def close_pull_request(
        self,
        repo: str,
        number: int,
        *,
        reason: str,
        actor: str,
        review_id: str,
    ) -> ClosedPullRequest: ...


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


def _as_iso(value: Any) -> str | None:
    """GitHub timestamps arrive as ISO strings; the demo path has datetimes. Normalise."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


class DryRunGitHubClient:
    name = "dry-run"
    dry_run = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.inbox = Path(self.settings.inbox_dir)
        self.outbox = Path(self.settings.outbox_dir)

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        fixture = self.inbox / _slug(repo) / f"{number}.json"
        if fixture.exists():
            payload = json.loads(fixture.read_text(encoding="utf-8"))
            return PullRequestInfo(
                repo=repo,
                number=number,
                title=payload.get("title", "(from fixture)"),
                author=payload.get("author", "unknown"),
                head_sha=payload.get("head_sha", DEMO_HEAD_SHA),
                base_sha=payload.get("base_sha", DEMO_BASE_SHA),
                diff=payload.get("diff", ""),
                changed_files=[ChangedFile(**f) for f in payload.get("changed_files", [])],
                draft=bool(payload.get("draft", False)),
                source="fixture",
            )
        return PullRequestInfo(
            repo=repo,
            number=number,
            title=DEMO_TITLE,
            author=DEMO_AUTHOR,
            head_sha=DEMO_HEAD_SHA,
            base_sha=DEMO_BASE_SHA,
            diff=DEMO_DIFF,
            changed_files=list(DEMO_CHANGED_FILES),
            source="demo",
        )

    async def list_repos(self) -> list[RepoSummary]:
        """The demo catalogue, so the guided flow is walkable before any token exists."""
        return [
            RepoSummary(
                full_name="acme/shop",
                description="Demo storefront -- the walkthrough repository",
                private=False,
                pushed_at=None,
            ),
            RepoSummary(
                full_name="acme/billing",
                description="Demo billing service",
                private=True,
                pushed_at=None,
            ),
        ]

    async def list_pulls(self, repo: str) -> list[PullRequestSummary]:
        """One demo PR for every repo, so the flow always has something to review."""
        return [
            PullRequestSummary(
                repo=repo,
                number=42,
                title=DEMO_TITLE,
                author=DEMO_AUTHOR,
                head_sha=DEMO_HEAD_SHA,
                draft=False,
                updated_at=_as_iso(datetime.now(UTC)),
            )
        ]

    async def validate(self) -> ConnectionCheck:
        return ConnectionCheck(ok=True, login="demo-user", name="dry-run", scopes=[])

    async def post_review(
        self,
        repo: str,
        number: int,
        *,
        head_sha: str,
        body: str,
        comments: list[ReviewComment],
    ) -> PostedReview:
        self.outbox.mkdir(parents=True, exist_ok=True)
        target = self.outbox / f"{_slug(repo)}_{number}_{head_sha[:8]}.json"
        payload: dict[str, Any] = {
            "repo": repo,
            "pr_number": number,
            "head_sha": head_sha,
            "event": "COMMENT",
            "note": "dry run: this review was NOT sent to GitHub",
            "body": body,
            "comments": [
                {"path": c.path, "line": c.line, "side": c.side, "body": c.body} for c in comments
            ],
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("dry-run review written", extra={"context": {"path": str(target)}})
        return PostedReview(
            id=f"dry-run-{number}-{head_sha[:8]}",
            url=None,
            comment_count=len(comments),
            posted=True,
            dry_run=True,
            summary=f"dry run: wrote {target}",
        )

    async def close_pull_request(
        self,
        repo: str,
        number: int,
        *,
        reason: str,
        actor: str,
        review_id: str,
    ) -> ClosedPullRequest:
        """Same behaviour as production, minus the network: the reason comment and
        the close land in the outbox so the rejection is inspectable offline."""
        self.outbox.mkdir(parents=True, exist_ok=True)
        target = self.outbox / f"{_slug(repo)}_{number}_closed.json"
        payload = {
            "repo": repo,
            "pr_number": number,
            "action": "close",
            "note": "dry run: this rejection was NOT sent to GitHub",
            "comment": REJECTION_HEADER + reason,
            "actor": actor,
            "review_id": review_id,
        }
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.info("dry-run rejection written", extra={"context": {"path": str(target)}})
        return ClosedPullRequest(
            commented=True,
            closed=True,
            dry_run=True,
            summary=f"dry run: wrote {target}",
        )


class LiveGitHubClient:
    """GitHub REST access with one token. Constructed with `token=` rather than reading
    settings, so the same class serves the configured token and a user-pasted connection
    without a second implementation to keep honest."""

    name = "github"
    dry_run = False

    def __init__(self, settings: Settings | None = None, *, token: str | None = None) -> None:
        self.settings = settings or get_settings()
        self._token = token if token is not None else self.settings.github_token
        self._breaker = get_breaker("github", failure_threshold=5)
        self._policy = RetryPolicy(
            attempts=self.settings.llm_max_retries, base_delay=0.5, max_delay=8.0, label="github"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _request(
        self, method: str, url: str, *, headers: dict[str, str] | None = None, json_body: Any = None
    ) -> httpx.Response:
        merged = {**self._headers, **(headers or {})}

        async def _call() -> httpx.Response:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.request(method, url, headers=merged, json=json_body)
                if response.status_code >= 500 or response.status_code == 429:
                    response.raise_for_status()
                return response

        return await self._breaker.call(lambda: call_with_retry(_call, policy=self._policy))

    async def validate(self) -> ConnectionCheck:
        """Who is this token, and what may it see? Raises GitHubError on bad tokens
        and on transport failure; the caller turns that into an HTTP response."""
        base = self.settings.github_api_base.rstrip("/")
        try:
            response = await self._request("GET", f"{base}/user")
        except Exception as exc:  # noqa: BLE001 - surface as GitHubError, not a raw transport error
            raise GitHubError(f"could not reach GitHub: {exc}", 503) from exc
        if response.status_code in (401, 403):
            raise GitHubError(
                "GitHub rejected the token (HTTP 401/403). "
                "Check that it is valid, unexpired, and has repo read access.",
                401,
            )
        if response.status_code >= 400:
            raise GitHubError(
                f"token check failed: HTTP {response.status_code}", response.status_code
            )
        data = response.json()
        raw_scopes = response.headers.get("x-oauth-scopes", "")
        scopes = [s.strip() for s in raw_scopes.split(",") if s.strip()]
        return ConnectionCheck(
            ok=True,
            login=data.get("login", "unknown"),
            name=data.get("name"),
            scopes=scopes,
        )

    async def list_repos(self) -> list[RepoSummary]:
        """Repos the token can reach, affiliation first so the user's own repos lead.
        Walks pagination up to a sane cap; GitHub returns newest-activity first."""
        base = self.settings.github_api_base.rstrip("/")
        out: list[RepoSummary] = []
        for page in range(1, 6):
            response = await self._request(
                "GET", f"{base}/user/repos?per_page=100&page={page}&sort=pushed"
            )
            if response.status_code >= 400:
                raise GitHubError(
                    f"failed to list repos: HTTP {response.status_code}", response.status_code
                )
            batch = response.json()
            out.extend(
                RepoSummary(
                    full_name=item["full_name"],
                    description=item.get("description"),
                    private=bool(item.get("private", False)),
                    pushed_at=item.get("pushed_at"),
                )
                for item in batch
            )
            if len(batch) < 100:
                break
        return out

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        base = self.settings.github_api_base.rstrip("/")
        errors: list[str] = []

        pr_response = await self._request("GET", f"{base}/repos/{repo}/pulls/{number}")
        if pr_response.status_code == 404:
            raise PullRequestUnavailable(
                f"pull request {repo}#{number} not found or not accessible", 404
            )
        if pr_response.status_code >= 400:
            raise GitHubError(
                f"failed to read {repo}#{number}: {pr_response.text[:200]}",
                pr_response.status_code,
            )
        pr = pr_response.json()

        diff = ""
        try:
            diff_response = await self._request(
                "GET",
                f"{base}/repos/{repo}/pulls/{number}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
            if diff_response.status_code < 400:
                diff = diff_response.text
            else:
                errors.append(f"diff unavailable: HTTP {diff_response.status_code}")
        except Exception as exc:  # noqa: BLE001 - metadata is still useful without a diff
            errors.append(f"diff unavailable: {exc}")

        files: list[ChangedFile] = []
        try:
            files_response = await self._request(
                "GET", f"{base}/repos/{repo}/pulls/{number}/files?per_page=100"
            )
            if files_response.status_code < 400:
                files = [
                    ChangedFile(
                        path=item["filename"],
                        status=item.get("status", "modified"),
                        additions=int(item.get("additions", 0)),
                        deletions=int(item.get("deletions", 0)),
                    )
                    for item in files_response.json()
                ]
            else:
                errors.append(f"file list unavailable: HTTP {files_response.status_code}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"file list unavailable: {exc}")

        return PullRequestInfo(
            repo=repo,
            number=number,
            title=pr.get("title", ""),
            author=(pr.get("user") or {}).get("login", "unknown"),
            head_sha=(pr.get("head") or {}).get("sha", ""),
            base_sha=(pr.get("base") or {}).get("sha", ""),
            diff=diff,
            changed_files=files,
            draft=bool(pr.get("draft", False)),
            source="api",
            errors=errors,
        )

    async def list_pulls(self, repo: str) -> list[PullRequestSummary]:
        """Open, non-draft PRs for one repo -- the list the console's picker shows."""
        base = self.settings.github_api_base.rstrip("/")
        out: list[PullRequestSummary] = []
        for page in range(1, 4):
            response = await self._request(
                "GET", f"{base}/repos/{repo}/pulls?state=open&per_page=100&page={page}"
            )
            if response.status_code >= 400:
                raise GitHubError(
                    f"failed to list pull requests for {repo}: HTTP {response.status_code}",
                    response.status_code,
                )
            batch = response.json()
            out.extend(
                PullRequestSummary(
                    repo=repo,
                    number=int(item["number"]),
                    title=item.get("title", ""),
                    author=(item.get("user") or {}).get("login", "unknown"),
                    head_sha=(item.get("head") or {}).get("sha", ""),
                    draft=bool(item.get("draft", False)),
                    updated_at=_as_iso(item.get("updated_at")),
                )
                for item in batch
            )
            if len(batch) < 100:
                break
        return out

    async def post_review(
        self,
        repo: str,
        number: int,
        *,
        head_sha: str,
        body: str,
        comments: list[ReviewComment],
    ) -> PostedReview:
        """Post a COMMENT review.

        Never APPROVE and never REQUEST_CHANGES. The agent reports; the decision to
        approve a change belongs to a human, and having the bot submit a blocking
        review state would silently transfer that decision to it.
        """
        base = self.settings.github_api_base.rstrip("/")
        payload = {
            "commit_id": head_sha,
            "body": body,
            "event": "COMMENT",
            "comments": [
                {"path": c.path, "line": c.line, "side": c.side or "RIGHT", "body": c.body}
                for c in comments
            ],
        }
        response = await self._request(
            "POST", f"{base}/repos/{repo}/pulls/{number}/reviews", json_body=payload
        )
        if response.status_code >= 400:
            raise GitHubError(
                f"failed to post review to {repo}#{number}: {response.text[:300]}",
                response.status_code,
            )
        data = response.json()
        return PostedReview(
            id=str(data.get("id", "")),
            url=data.get("html_url"),
            comment_count=len(comments),
            posted=True,
            dry_run=False,
            summary=f"posted {len(comments)} comment(s)",
        )

    async def close_pull_request(
        self,
        repo: str,
        number: int,
        *,
        reason: str,
        actor: str,
        review_id: str,
    ) -> ClosedPullRequest:
        """Post the rejection reason as an issue comment, then close the PR.

        The comment goes first: if the token can comment but not close (an unusual
        permission split), the author still gets the reason. If it can close but not
        comment, closing silently with no explanation would be worse than not
        closing, so that failure aborts the whole action and the caller records a
        rejection with no GitHub side effect.
        """
        base = self.settings.github_api_base.rstrip("/")

        comment_response = await self._request(
            "POST",
            f"{base}/repos/{repo}/issues/{number}/comments",
            json_body={"body": REJECTION_HEADER + reason},
        )
        if comment_response.status_code >= 400:
            raise GitHubError(
                f"failed to post rejection comment on {repo}#{number}: "
                f"{comment_response.text[:300]}",
                comment_response.status_code,
            )
        comment_url: str | None = comment_response.json().get("html_url")

        close_response = await self._request(
            "PATCH", f"{base}/repos/{repo}/pulls/{number}", json_body={"state": "closed"}
        )
        if close_response.status_code >= 400:
            raise GitHubError(
                f"posted the rejection comment, but failed to close {repo}#{number}: "
                f"{close_response.text[:300]} -- close it manually",
                close_response.status_code,
            )

        return ClosedPullRequest(
            commented=True,
            closed=True,
            comment_url=comment_url,
            summary="posted the rejection reason and closed the pull request",
        )


def get_github_client(settings: Settings | None = None) -> GitHubClient:
    resolved = settings or get_settings()
    if resolved.github_client_mode == "live":
        return LiveGitHubClient(resolved)
    return DryRunGitHubClient(resolved)


class ResolvingGitHubClient:
    """The client everything runs through.

    Every call resolves a LiveGitHubClient against the connection store at call time:
    a stored user connection wins, otherwise it falls back to the settings-configured
    client. Per-call resolution (not cached at construction) is the whole point: the
    moment a token is connected from the console, the running API *and* the running
    worker both use it -- a worker that started before the connection still reviews
    the real pull request, because resolution happens when the job runs, not when the
    process booted.

    Within one review the resolution is stable in practice (connections change only
    when a human acts), and the supersede checks make a mid-review change harmless
    rather than corrupting.
    """

    name = "resolving"
    #: Reported pre-resolution for the startup log only; /api/github/status is the
    #: authoritative answer because it asks the store directly.
    dry_run = False

    def __init__(self, store: ConnectionStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()

    async def _client(self) -> GitHubClient:
        connection = await self._store.get_active()
        if connection is not None:
            return LiveGitHubClient(self._settings, token=connection.token)
        fallback = get_github_client(self._settings)
        self.dry_run = bool(getattr(fallback, "dry_run", True))
        return fallback

    async def list_repos(self) -> list[RepoSummary]:
        return await (await self._client()).list_repos()

    async def list_pulls(self, repo: str) -> list[PullRequestSummary]:
        return await (await self._client()).list_pulls(repo)

    async def validate(self) -> ConnectionCheck:
        return await (await self._client()).validate()

    async def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
        return await (await self._client()).get_pull_request(repo, number)

    async def post_review(
        self,
        repo: str,
        number: int,
        *,
        head_sha: str,
        body: str,
        comments: list[ReviewComment],
    ) -> PostedReview:
        return await (await self._client()).post_review(
            repo, number, head_sha=head_sha, body=body, comments=comments
        )

    async def close_pull_request(
        self,
        repo: str,
        number: int,
        *,
        reason: str,
        actor: str,
        review_id: str,
    ) -> ClosedPullRequest:
        return await (await self._client()).close_pull_request(
            repo, number, reason=reason, actor=actor, review_id=review_id
        )
