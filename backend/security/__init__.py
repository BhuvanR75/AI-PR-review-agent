"""Security controls that are not capabilities: prompt-injection guarding, secret
redaction, and the threat model as data."""

from __future__ import annotations

from backend.security.injection_guard import (
    REDACTION_TEMPLATE,
    UNTRUSTED_INPUT_CLAUSE,
    Flag,
    GuardReport,
    guard_payload,
    guard_text,
    redact_credentials,
    scan_untrusted,
)
from backend.security.threat_model import THREATS, Threat

__all__ = [
    "REDACTION_TEMPLATE",
    "THREATS",
    "UNTRUSTED_INPUT_CLAUSE",
    "Flag",
    "GuardReport",
    "Threat",
    "guard_payload",
    "guard_text",
    "redact_credentials",
    "scan_untrusted",
]
