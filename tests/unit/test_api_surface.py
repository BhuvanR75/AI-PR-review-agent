"""The HTTP surface, checked without a server or a database.

A router that is silently not registered is invisible: the app starts, the tests pass,
and the console 404s in production. Asserting the OpenAPI paths catches exactly that,
and it costs nothing because building the schema touches no infrastructure.
"""

from __future__ import annotations

import pytest

from backend.app import create_app

#: Every path the console and the CLI depend on. If one of these disappears, something
#: that used to work stops working, so it belongs in a test rather than in a release note.
REQUIRED_PATHS = {
    "/webhooks/github",
    "/healthz",
    "/readyz",
    "/api/diagnostics",
    "/api/reviews",
    "/api/reviews/{review_id}",
    "/api/reviews/{review_id}/trace",
    "/api/reviews/{review_id}/explain",
    "/api/hitl/queue",
    "/api/hitl/pending",
    "/api/hitl/{review_id}/approve",
    "/api/hitl/{review_id}/reject",
    "/api/hitl/{review_id}/rerun",
    "/api/queue/status",
    "/api/economics/summary",
    "/api/economics/feedback",
    "/api/economics/advice",
    "/api/economics/alerts",
    "/api/meta/agents",
    "/api/meta/tools",
    "/api/meta/capabilities",
    "/api/meta/threat-model",
}


@pytest.fixture(scope="module")
def schema() -> dict:
    return create_app().openapi()


def test_every_required_path_is_registered(schema: dict) -> None:
    missing = REQUIRED_PATHS - set(schema["paths"])
    assert not missing, f"these routes are not registered: {sorted(missing)}"


def test_publication_and_decisions_are_post_only(schema: dict) -> None:
    """A GET that changes state would be reachable by any browser or prefetcher."""
    for path in (
        "/api/hitl/{review_id}/approve",
        "/api/hitl/{review_id}/reject",
        "/api/hitl/{review_id}/rerun",
        "/webhooks/github",
    ):
        methods = set(schema["paths"][path])
        assert methods == {"post"}, f"{path} should be POST-only, got {sorted(methods)}"


def test_the_approval_surface_is_not_a_single_endpoint(schema: dict) -> None:
    """Approval, rejection and re-run are distinct actions with distinct feedback, so
    they must not be collapsed into one 'status' setter."""
    for path in (
        "/api/hitl/{review_id}/approve",
        "/api/hitl/{review_id}/reject",
        "/api/hitl/{review_id}/rerun",
    ):
        assert path in schema["paths"]
