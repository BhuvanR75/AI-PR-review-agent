"""Webhook authenticity.

The signature is computed over the *raw* request body, before any parsing. That
ordering is the whole point: re-serializing a parsed payload changes byte-for-byte
whitespace and key order, and the HMAC will not match. So the route reads `bytes` and
passes them here untouched.

Comparison is constant-time. A byte-by-byte early-exit comparison leaks how many
leading bytes of a forged signature were correct, which is enough to forge one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"

SIGNATURE_PREFIX = "sha256="


class SignatureError(Exception):
    """The payload is not provably from GitHub."""


def compute_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(*, secret: str, body: bytes, signature_header: str | None) -> None:
    if not secret:
        raise SignatureError("no webhook secret configured; refusing to trust the payload")
    if not signature_header:
        raise SignatureError(f"missing {SIGNATURE_HEADER} header")

    expected = compute_signature(secret, body)
    if not hmac.compare_digest(expected, signature_header.strip()):
        logger.warning("rejected webhook with an invalid signature")
        raise SignatureError("signature does not match")


def require_headers(*, delivery_id: str | None, event_type: str | None) -> tuple[str, str]:
    missing: list[str] = []
    if not delivery_id:
        missing.append(DELIVERY_HEADER)
    if not event_type:
        missing.append(EVENT_HEADER)
    if missing:
        raise SignatureError(f"missing required header(s): {', '.join(missing)}")
    assert delivery_id is not None and event_type is not None  # noqa: S101
    return delivery_id, event_type
