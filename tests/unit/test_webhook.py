"""Ingress is the trust boundary, so it gets hostile-input tests rather than happy-path
ones: a tampered body, a swapped signature, a payload with the head commit removed."""

from __future__ import annotations

import pytest

from backend.webhook_receiver.parser import (
    PayloadError,
    parse_pull_request_event,
    skip_reason,
    summarize,
)
from backend.webhook_receiver.validator import (
    SignatureError,
    compute_signature,
    require_headers,
    verify_signature,
)

SECRET = "dev-webhook-secret"


def payload(**overrides: object) -> dict:
    base = {
        "action": "opened",
        "number": 42,
        "repository": {"full_name": "acme/shop"},
        "pull_request": {
            "number": 42,
            "title": "Add refunds",
            "draft": False,
            "user": {"login": "dev"},
            "head": {"sha": "abc123"},
            "base": {"sha": "def456"},
        },
    }
    base.update(overrides)
    return base


# --- signature ---------------------------------------------------------------
def test_valid_signature_is_accepted() -> None:
    body = b'{"hello":"world"}'
    verify_signature(
        secret=SECRET, body=body, signature_header=compute_signature(SECRET, body)
    )


def test_tampered_body_is_rejected() -> None:
    body = b'{"hello":"world"}'
    signature = compute_signature(SECRET, body)
    with pytest.raises(SignatureError):
        verify_signature(
            secret=SECRET, body=b'{"hello":"world"} ', signature_header=signature
        )


def test_wrong_secret_is_rejected() -> None:
    body = b"{}"
    with pytest.raises(SignatureError):
        verify_signature(
            secret=SECRET, body=body, signature_header=compute_signature("other", body)
        )


def test_missing_signature_header_is_rejected() -> None:
    with pytest.raises(SignatureError):
        verify_signature(secret=SECRET, body=b"{}", signature_header=None)


def test_unsigned_payload_is_refused_when_no_secret_is_configured() -> None:
    """An empty secret must not mean "trust everything"."""
    with pytest.raises(SignatureError):
        verify_signature(secret="", body=b"{}", signature_header="sha256=deadbeef")


def test_signature_is_hex_sha256_with_prefix() -> None:
    signature = compute_signature(SECRET, b"{}")
    assert signature.startswith("sha256=")
    assert len(signature) == len("sha256=") + 64


def test_required_headers() -> None:
    assert require_headers(delivery_id="d-1", event_type="pull_request") == (
        "d-1",
        "pull_request",
    )
    with pytest.raises(SignatureError):
        require_headers(delivery_id=None, event_type="pull_request")
    with pytest.raises(SignatureError):
        require_headers(delivery_id="d-1", event_type=None)


# --- parsing -----------------------------------------------------------------
def test_parses_a_pull_request_event() -> None:
    event = parse_pull_request_event(payload(), delivery_id="delivery-1")
    assert event.repo == "acme/shop"
    assert event.pr_number == 42
    assert event.head_sha == "abc123"
    assert event.base_sha == "def456"
    assert event.author == "dev"
    assert event.is_reviewable


@pytest.mark.parametrize("missing", ["repository", "pull_request"])
def test_missing_top_level_sections_raise(missing: str) -> None:
    broken = payload()
    broken.pop(missing)
    with pytest.raises(PayloadError):
        parse_pull_request_event(broken, delivery_id="d")


def test_missing_head_sha_raises_rather_than_defaulting() -> None:
    broken = payload()
    broken["pull_request"]["head"] = {}
    with pytest.raises(PayloadError):
        parse_pull_request_event(broken, delivery_id="d")


def test_missing_number_raises() -> None:
    broken = payload()
    broken["pull_request"].pop("number")
    broken.pop("number")
    with pytest.raises(PayloadError):
        parse_pull_request_event(broken, delivery_id="d")


@pytest.mark.parametrize("action", ["labeled", "assigned", "edited", "closed"])
def test_non_reviewable_actions_are_skipped(action: str) -> None:
    event = parse_pull_request_event(payload(action=action), delivery_id="d")
    assert not event.is_reviewable
    assert skip_reason(event) is not None


def test_draft_prs_are_skipped() -> None:
    marked = payload()
    marked["pull_request"]["draft"] = True
    event = parse_pull_request_event(marked, delivery_id="d")
    assert skip_reason(event) == "pull request is a draft"


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_reviewable_actions_pass(action: str) -> None:
    event = parse_pull_request_event(payload(action=action), delivery_id="d")
    assert skip_reason(event) is None


def test_supersede_key_is_per_commit() -> None:
    first = parse_pull_request_event(payload(), delivery_id="d1")
    second = parse_pull_request_event(
        payload(pull_request={**payload()["pull_request"], "head": {"sha": "zzz"}}),
        delivery_id="d2",
    )
    assert first.supersede_key != second.supersede_key
    assert first.supersede_key.endswith("@abc123")


def test_summarize_mentions_reviewability() -> None:
    event = parse_pull_request_event(payload(), delivery_id="d")
    text = summarize(event)
    assert "acme/shop#42" in text
    assert "reviewable=True" in text
