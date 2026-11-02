"""The threat model, as data.

A threat model in prose rots the moment someone moves a file. This one names the module
that implements each control, and a unit test asserts those paths still exist -- so the
document fails the build when it stops being true, which is the only way documentation
about security stays honest.

Read `residual_risk` as the part that is *not* handled. Every entry has one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Threat:
    id: str
    name: str
    description: str
    control: str
    where: tuple[str, ...]
    residual_risk: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "control": self.control,
            "where": list(self.where),
            "residual_risk": self.residual_risk,
        }


THREATS: tuple[Threat, ...] = (
    Threat(
        id="T1",
        name="Prompt injection through untrusted text",
        description=(
            "A diff comment or a retrieved code comment instructs the model to ignore "
            "its rules, to report the change as safe, or to reveal its prompt."
        ),
        control=(
            "Untrusted text is scanned for instruction-shaped spans before the prompt is "
            "assembled; flags are recorded on the events spine and surfaced as warnings "
            "on the review. The system prompt states that diff and context are data. "
            "Detection is not deletion -- a hostile comment stays visible to the reviewer."
        ),
        where=("backend/security/injection_guard.py", "backend/agents/base_agent.py"),
        residual_risk=(
            "A payload that resembles none of the patterns still reaches the model. The "
            "human gate is the control that carries this, not the pattern list."
        ),
    ),
    Threat(
        id="T2",
        name="Credential in a diff leaving the machine",
        description=(
            "A contributor commits a live key; the raw diff is then sent to a third-party "
            "model provider and echoed into a finding's rationale, the database and a "
            "GitHub comment."
        ),
        control=(
            "Credential-shaped literals are redacted to a marker before the prompt is "
            "built, so the value never reaches the provider and never lands in the "
            "finding text. The marker keeps the finding raisable, and the review reports "
            "that a redaction happened so a reviewer is not misled."
        ),
        where=("backend/security/injection_guard.py", "backend/agents/base_agent.py"),
        residual_risk=(
            "Redaction is pattern-based: a credential that does not look like one at the "
            "assignment site is not caught. Rotation on discovery is still the remedy."
        ),
    ),
    Threat(
        id="T3",
        name="The agent posts without a human",
        description=(
            "A model or a misconfiguration causes a review to be published, or a review "
            "to be approved or blocked on the team's behalf."
        ),
        control=(
            "HITL_MODE=always by default; publication goes through one function that "
            "requires the POST_REVIEW capability, which no specialist holds; the review "
            "is always submitted as a COMMENT, never as APPROVE or REQUEST_CHANGES."
        ),
        where=("backend/hitl/publishing.py", "backend/tools/capability_scope.py"),
        residual_risk=(
            "Confidence-gated auto-post exists behind a setting. It is off, and it stays "
            "off until approval data justifies it."
        ),
    ),
    Threat(
        id="T4",
        name="Approving code that no longer exists",
        description=(
            "A reviewer approves a review of an old commit, so a human's sign-off is "
            "attached to code that was never reviewed."
        ),
        control=(
            "One review per (repo, pr, head_sha). A new push supersedes the previous "
            "head, closes its queue entry, and publication re-checks the head sha at the "
            "moment of posting."
        ),
        where=("backend/database/repository.py", "backend/hitl/publishing.py"),
        residual_risk=(
            "A commit pushed between the final check and the API call is not detected; "
            "GitHub rejects the comment rather than attaching it to the wrong commit."
        ),
    ),
    Threat(
        id="T5",
        name="A finding about code the agent never saw",
        description=(
            "The model hallucinates a plausible issue in a file outside the diff, or on a "
            "line the change did not touch."
        ),
        control=(
            "Structural anchoring, not prompt engineering: a finding for a file outside "
            "the diff is dropped with a recorded reason, and a finding whose line is not "
            "among the added lines loses its anchor so it cannot become a silently "
            "rejected inline comment."
        ),
        where=("backend/agents/base_agent.py", "backend/tools/diffparse.py"),
        residual_risk=(
            "A confidently wrong finding *on* an added line is indistinguishable from a "
            "correct one. Confidence, rationale and the human gate are what remain."
        ),
    ),
    Threat(
        id="T6",
        name="Unauthenticated access to the approval surface",
        description=(
            "Anything that can reach the API can read repository content and post reviews "
            "as though a human had approved them."
        ),
        control=(
            "A shared bearer token on every route except the webhook, which is instead "
            "authenticated by an HMAC over the raw body, compared in constant time; "
            "tokens are compared with secrets.compare_digest."
        ),
        where=("backend/api/auth.py", "backend/webhook_receiver/validator.py"),
        residual_risk=(
            "One shared token, no per-user identity, no roles, and the console's token "
            "lives in the browser bundle. Not fit for a multi-user or public deployment."
        ),
    ),
    Threat(
        id="T7",
        name="Unbounded spend",
        description=(
            "A retry loop, a large diff or a stuck queue runs the LLM bill past what "
            "anyone agreed to."
        ),
        control=(
            "BudgetGuard checks the daily spend before every model call and hard-blocks; "
            "per-call timeouts, bounded retries and a circuit breaker per provider bound "
            "the blast radius of a single failure."
        ),
        where=("backend/economics/budget.py", "backend/reliability/circuit_breaker.py"),
        residual_risk=(
            "The cap is daily, not per review or per repository, so one pathological PR "
            "can consume the whole day's budget."
        ),
    ),
    Threat(
        id="T8",
        name="A silently incomplete audit trail",
        description=(
            "The events spine stops recording, and an unlogged review is mistaken for a "
            "trustworthy one."
        ),
        control=(
            "Spine writes never raise -- refusing to review would be worse -- but every "
            "failure is counted, logged at ERROR, and reported by /readyz, "
            "/api/diagnostics and an alert rule."
        ),
        where=("backend/observability/events.py", "backend/observability/alerting.py"),
        residual_risk=(
            "Losing the spine write loses the record permanently. There is no local "
            "buffer that replays."
        ),
    ),
)


def as_dict() -> dict[str, object]:
    return {
        "threats": [threat.as_dict() for threat in THREATS],
        "note": (
            "Every entry names the module that implements its control, and a unit test "
            "asserts those paths exist. residual_risk is the part that is not handled."
        ),
    }


__all__ = ["THREATS", "Threat", "as_dict"]
