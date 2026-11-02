"""Shapes that cross the GitHub boundary."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.models.review import ChangedFile


@dataclass(frozen=True)
class PullRequestInfo:
    repo: str
    number: int
    title: str
    author: str
    head_sha: str
    base_sha: str
    diff: str
    changed_files: list[ChangedFile] = field(default_factory=list)
    draft: bool = False
    source: str = "api"
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReviewComment:
    """An inline comment. `line` is a line number on the new side of the diff, which
    is why every finding's line is validated against the diff before it gets here."""

    path: str
    line: int
    body: str
    side: str = "RIGHT"


@dataclass(frozen=True)
class RepoSummary:
    """A repository the connected token can see, for the console's picker."""

    full_name: str
    description: str | None = None
    private: bool = False
    pushed_at: str | None = None


@dataclass(frozen=True)
class PullRequestSummary:
    """An open PR, for the console's picker."""

    repo: str
    number: int
    title: str
    author: str
    head_sha: str
    draft: bool = False
    updated_at: str | None = None


@dataclass(frozen=True)
class ConnectionCheck:
    ok: bool
    login: str
    name: str | None = None
    scopes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PostedReview:
    id: str
    url: str | None
    comment_count: int
    posted: bool
    dry_run: bool = False
    summary: str = ""


@dataclass(frozen=True)
class ClosedPullRequest:
    """Outcome of a rejection: the reason comment and the close, separately, because
    either can succeed without the other and the audit trail records both."""

    commented: bool
    closed: bool
    dry_run: bool = False
    comment_url: str | None = None
    summary: str = ""
