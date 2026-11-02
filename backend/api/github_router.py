"""The GitHub connection and browsing API.

This is the front door of the user-initiated flow: check status, connect a token,
list repos, list open PRs. Everything here is dynamic -- connecting takes effect in
the running API immediately because listing always resolves the current connection
at call time.

The connect endpoint validates the token against GitHub *before* storing it, so a
typo can never become the active connection, and the response carries the login and
scopes the token actually has rather than what the user assumed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from backend.core.exceptions import GitHubError
from backend.integrations.connection_store import ConnectionStore, token_fingerprint
from backend.integrations.github_client import LiveGitHubClient
from backend.integrations.github_models import ConnectionCheck
from backend.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["github"])


class ConnectRequest(BaseModel):
    token: str = Field(
        min_length=8,
        description="A GitHub personal access token (classic or fine-grained).",
    )


class ConnectResponse(BaseModel):
    connected: bool
    login: str
    name: str | None = None
    scopes: list[str] = Field(default_factory=list)
    fingerprint: str
    dry_run: bool = False
    message: str = ""


class ConnectionStatus(BaseModel):
    connected: bool
    login: str | None = None
    scopes: list[str] = Field(default_factory=list)
    fingerprint: str | None = None
    mode: str = "dry_run"
    message: str = ""


def _store(request: Request) -> ConnectionStore:
    settings = get_settings()
    return ConnectionStore(settings.database_url)


def _get_context(request: Request):
    context = getattr(request.app.state, "context", None)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="application context is not initialised",
        )
    return context


@router.get("/status", response_model=ConnectionStatus)
async def connection_status(request: Request) -> ConnectionStatus:
    settings = get_settings()
    public = await _store(request).describe()
    return ConnectionStatus(
        connected=public is not None,
        login=public.user_login if public else None,
        scopes=public.scopes if public else [],
        fingerprint=public.fingerprint if public else None,
        mode="live" if public else settings.github_client_mode,
        message=(
            f"connected as {public.user_login} (live GitHub; token {public.fingerprint})"
            if public
            else "not connected"
        ),
    )


@router.post("/connect", response_model=ConnectResponse)
async def connect(request: Request, payload: ConnectRequest) -> ConnectResponse:
    """Validate a token against GitHub and store it as the active connection."""
    settings = get_settings()

    # The validation call needs a live client regardless of GITHUB_CLIENT_MODE: the
    # whole point of connecting is to talk to real GitHub.
    client = LiveGitHubClient(settings, token=payload.token.strip())
    try:
        check: ConnectionCheck = await client.validate()
    except GitHubError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502,
            detail=str(exc),
        ) from exc

    await _store(request).save(
        token=payload.token.strip(),
        user_login=check.login,
        scopes=check.scopes,
        api_base=settings.github_api_base,
    )
    logger.info(
        "github connected from console",
        extra={"context": {"user": check.login, "fingerprint": token_fingerprint(check.login)}},
    )
    return ConnectResponse(
        connected=True,
        login=check.login,
        name=check.name,
        scopes=check.scopes,
        fingerprint=token_fingerprint(payload.token.strip()),
        dry_run=False,
        message=f"Connected as {check.login}. Repo and PR listing are live.",
    )


@router.post("/disconnect")
async def disconnect(request: Request) -> dict[str, object]:
    deleted = await _store(request).delete()
    return {"connected": False, "deleted": deleted, "message": "disconnected"}


@router.get("/repos")
async def list_repos(request: Request) -> dict[str, object]:
    """Repos visible to the active connection (or the demo catalogue when offline)."""
    context = _get_context(request)
    try:
        repos = await context.github.list_repos()
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    allowlist = context.settings.repo_allowlist_set
    return {
        "repos": [
            {
                "full_name": r.full_name,
                "description": r.description,
                "private": r.private,
                "pushed_at": r.pushed_at,
                "allowed": (not allowlist) or r.full_name in allowlist,
            }
            for r in repos
        ],
        "source": "dry-run" if getattr(context.github, "dry_run", False) else "github",
    }


@router.get("/repos/{owner}/{name}/pulls")
async def list_pulls(request: Request, owner: str, name: str) -> dict[str, object]:
    """Open PRs for one repo, newest first."""
    context = _get_context(request)
    repo = f"{owner}/{name}"
    try:
        pulls = await context.github.list_pulls(repo)
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "repo": repo,
        "pulls": [
            {
                "repo": p.repo,
                "number": p.number,
                "title": p.title,
                "author": p.author,
                "head_sha": p.head_sha,
                "draft": p.draft,
                "updated_at": p.updated_at,
            }
            for p in pulls
        ],
        "source": "dry-run" if getattr(context.github, "dry_run", False) else "github",
    }
