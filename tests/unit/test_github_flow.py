"""The user-initiated flow: connection store, dynamic resolution, listing, trigger.

These tests exercise the parts most likely to rot silently: the store's public
surface never leaking the token, the resolver picking the live client the moment a
connection exists (so a worker started before connecting still reviews real PRs),
and the trigger endpoint's guard rails.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import httpx
import pytest

from backend.integrations.connection_store import Connection, ConnectionStore, token_fingerprint
from backend.integrations.github_client import (
    DryRunGitHubClient,
    LiveGitHubClient,
    ResolvingGitHubClient,
)
from backend.integrations.github_models import RepoSummary
from backend.models.review import ChangedFile
from backend.settings import get_settings

# --- fingerprint -------------------------------------------------------------


def test_token_fingerprint_is_short_and_not_the_token() -> None:
    fp = token_fingerprint("ghp_supersecret")
    assert len(fp) == 8
    assert "supersecret" not in fp
    assert token_fingerprint("ghp_supersecret") == fp  # stable


# --- store -------------------------------------------------------------------


class FakeAsyncConn:
    """Records execute calls; fetchrow returns the saved row."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.saved: list[tuple] = []

    async def execute(self, query, *args):  # type: ignore[no-untyped-def]
        self.saved.append((" ".join(query.split()), args))
        return "DELETE 1"

    async def fetchrow(self, query, *args):  # type: ignore[no-untyped-def]
        _ = (" ".join(query.split()), args)  # recorded implicitly via saved
        return self.rows[0] if self.rows else None

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
        return False

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_store_describe_never_returns_the_token(fake_pool, monkeypatch) -> None:
    store = ConnectionStore("postgresql://unused")
    row = {
        "id": uuid.uuid4(),
        "label": "default",
        "user_login": "octocat",
        "scopes": ["repo"],
        "api_base": "https://api.github.com",
        "connected_by": "console",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "token": "ghp_supersecret",
    }

    async def fake_connect():  # type: ignore[no-untyped-def]
        return FakeAsyncConn([row])

    monkeypatch.setattr(store, "_connect", fake_connect)
    public = await store.describe()
    assert public is not None
    assert public.user_login == "octocat"
    assert public.fingerprint == token_fingerprint("ghp_supersecret")
    assert "ghp_supersecret" not in str(public)


@pytest.mark.asyncio
async def test_store_get_active_roundtrip(fake_pool, monkeypatch) -> None:
    store = ConnectionStore("postgresql://unused")
    row = {
        "label": "default",
        "user_login": "octocat",
        "token": "ghp_abc12345",
        "scopes": ["repo"],
        "api_base": "https://api.github.com",
    }

    async def fake_connect():  # type: ignore[no-untyped-def]
        return FakeAsyncConn([row])

    monkeypatch.setattr(store, "_connect", fake_connect)
    conn = await store.get_active()
    assert conn is not None
    assert conn.user_login == "octocat"
    assert conn.token == "ghp_abc12345"


# --- resolver ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_prefers_the_stored_connection(monkeypatch) -> None:
    settings = get_settings()
    store = ConnectionStore(settings.database_url)
    resolver = ResolvingGitHubClient(store, settings)

    async def fake_get_active():  # type: ignore[no-untyped-def]
        return Connection(
            label="default",
            user_login="octocat",
            token="ghp_live_token_1",
            scopes=["repo"],
            api_base="https://api.github.com",
        )

    monkeypatch.setattr(store, "get_active", fake_get_active)
    with patch.object(LiveGitHubClient, "list_repos") as live_list:
        live_list.return_value = [RepoSummary(full_name="octo/repo")]
        repos = await resolver.list_repos()
    assert [r.full_name for r in repos] == ["octo/repo"]
    live_list.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolver_falls_back_without_a_connection(monkeypatch) -> None:
    settings = get_settings()
    store = ConnectionStore(settings.database_url)
    resolver = ResolvingGitHubClient(store, settings)

    async def none():  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(store, "get_active", none)
    dry = DryRunGitHubClient(settings)
    with patch("backend.integrations.github_client.get_github_client") as fallback:
        fallback.return_value = dry
        repos = await resolver.list_repos()
    assert repos  # the dry-run demo catalogue
    fallback.assert_called_once()


@pytest.mark.asyncio
async def test_resolver_get_pull_request_is_dynamic(monkeypatch) -> None:
    """The worker path: a review job runs through get_pull_request at execution time,
    so resolution must be per-call, not frozen at boot."""
    settings = get_settings()
    store = ConnectionStore(settings.database_url)
    resolver = ResolvingGitHubClient(store, settings)

    async def fake_get_active():  # type: ignore[no-untyped-def]
        return Connection(
            label="default",
            user_login="octocat",
            token="ghp_live_token_1",
            scopes=["repo"],
            api_base="https://api.github.com",
        )

    monkeypatch.setattr(store, "get_active", fake_get_active)
    with patch.object(LiveGitHubClient, "get_pull_request") as live_get:
        live_get.return_value = object()  # sentinel: identity proves it was used
        info = await resolver.get_pull_request("octo/repo", 1)
    assert info is live_get.return_value


# --- dry-run client listing ----------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_lists_demo_catalogue() -> None:
    settings = get_settings()
    client = DryRunGitHubClient(settings)
    repos = await client.list_repos()
    assert any(r.full_name == "acme/shop" for r in repos)
    pulls = await client.list_pulls("acme/shop")
    assert pulls and pulls[0].number == 42
    check = await client.validate()
    assert check.ok and check.login == "demo-user"


@pytest.mark.asyncio
async def test_live_rejection_comments_then_closes_pull_request() -> None:
    settings = get_settings()
    client = LiveGitHubClient(settings, token="ghp_test_token")
    calls: list[tuple[str, str, object]] = []
    responses = [
        httpx.Response(201, json={"html_url": "https://github.com/acme/shop/issues/42#issuecomment-1"}),
        httpx.Response(200, json={"state": "closed"}),
    ]

    async def request(method: str, url: str, *, headers=None, json_body=None) -> httpx.Response:
        calls.append((method, url, json_body))
        return responses.pop(0)

    client._request = request  # type: ignore[method-assign]

    result = await client.close_pull_request(
        "acme/shop",
        42,
        reason="duplicates #17",
        actor="reviewer",
        review_id="review-1",
    )

    assert result.closed is True
    assert calls == [
        (
            "POST",
            "https://api.github.com/repos/acme/shop/issues/42/comments",
            {"body": "## Review rejected\n\nA human reviewer rejected this pull request after an automated review. Reason:\n\n> duplicates #17"},
        ),
        (
            "PATCH",
            "https://api.github.com/repos/acme/shop/pulls/42",
            {"state": "closed"},
        ),
    ]


# --- API surface ---------------------------------------------------------------


@pytest.fixture(scope="module")
def schema() -> dict:
    from backend.app import create_app

    return create_app().openapi()


def test_github_flow_routes_are_registered(schema: dict) -> None:
    expected = {
        "/api/github/status",
        "/api/github/connect",
        "/api/github/disconnect",
        "/api/github/repos",
        "/api/github/repos/{owner}/{name}/pulls",
        "/api/reviews/trigger",
    }
    missing = expected - set(schema["paths"])
    assert not missing, f"missing routes: {sorted(missing)}"


def test_connect_is_post_only(schema: dict) -> None:
    assert set(schema["paths"]["/api/github/connect"]) == {"post"}
    assert set(schema["paths"]["/api/reviews/trigger"]) == {"post"}


# --- artifact repository -------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_save_and_get_roundtrip(fake_pool, monkeypatch) -> None:
    from backend.database.artifacts import ArtifactRepository

    repo = ArtifactRepository("postgresql://unused")
    saved: dict = {}

    class FakeConn:
        async def execute(self, query, *args):  # type: ignore[no-untyped-def]
            saved["args"] = args
            return "INSERT 0 1"

        async def fetchrow(self, query, *args):  # type: ignore[no-untyped-def]
            return {
                "diff": saved.get("args", (None, None))[1] or "",
                "changed_files": saved.get("args", (None, None, "[]"))[2] or "[]",
            }

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *exc):  # type: ignore[no-untyped-def]
            return False

        async def close(self) -> None:
            return None

    async def fake_connect():  # type: ignore[no-untyped-def]
        return FakeConn()

    monkeypatch.setattr(repo, "_connect", fake_connect)
    review_id = str(uuid.uuid4())
    await repo.save(review_id, diff="diff --git a/x b/x", changed_files=[ChangedFile(path="x")])
    artifact = await repo.get(review_id)
    assert artifact is not None
    assert "diff --git" in artifact.diff
    assert artifact.changed_files[0].path == "x"
