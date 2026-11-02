"""The shared shape of a specialist pass.

Every specialist does the same seven things, and only two of them are domain-specific
(the system prompt and which findings count as on-topic). That is deliberate: if the
four passes drift in how they gather context, bound their runtime, or record evidence,
the system stops being auditable as a whole.

The load-bearing step is `anchor_findings`. A model can and will report a finding
about code it did not actually see -- a plausible-sounding issue in a file that is not
in the diff. That is hallucination in a critical path, and the defense is structural
rather than prompt-based: a finding about a file outside the diff is dropped, and one
whose line is not among the diff's added lines loses its line anchor (so it can never
become a silently-rejected inline comment on GitHub).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import ClassVar

from backend.economics.budget import BudgetGuard
from backend.memory.code_memory import CodeMemory
from backend.memory.context_retriever import RetrievedChunk
from backend.models.enums import AgentType, EventType, Outcome
from backend.models.findings import Finding, FindingsPayload, SpecialistResult
from backend.models.review import ChangedFile
from backend.observability.events import Span
from backend.prompts.registry import PromptRegistry, get_prompt_registry
from backend.reliability.circuit_breaker import CircuitOpenError
from backend.reliability.timeout import TimeoutExceeded, call_with_timeout
from backend.security.injection_guard import UNTRUSTED_INPUT_CLAUSE, guard_payload
from backend.settings import Settings, get_settings
from backend.tools.capability_scope import CapabilityScope, scope_for
from backend.tools.diffparse import parse_added_lines
from backend.tools.llm_client import LLMClient, get_llm_client
from backend.tools.model_router import ModelRouter, get_model_router

logger = logging.getLogger(__name__)

MAX_DIFF_CHARS = 40_000
MAX_CONTEXT_CHARS = 18_000


@dataclass
class SpecialistInput:
    """Everything one pass needs. The diff and the retrieved context are shared by
    all four passes, fetched once by the orchestrator's prepare node."""

    review_id: str
    repo: str
    pr_number: int
    head_sha: str
    diff: str
    changed_files: list[ChangedFile] = field(default_factory=list)
    title: str | None = None
    context: list[RetrievedChunk] = field(default_factory=list)


def anchor_findings(
    findings: list[Finding],
    inp: SpecialistInput,
    *,
    agent_type: AgentType,
) -> tuple[list[Finding], list[str]]:
    """Keep findings that point at code this diff actually touched.

    Returns the kept findings and the list of reasons things were dropped, which are
    recorded as errors on the result rather than discarded silently.
    """
    changed = {f.path for f in inp.changed_files}
    added_by_path: dict[str, set[int]] = {}
    for line in parse_added_lines(inp.diff):
        added_by_path.setdefault(line.path, set()).add(line.line_no)
    if not changed:
        changed = set(added_by_path)

    kept: list[Finding] = []
    notes: list[str] = []

    for finding in findings:
        if changed and finding.file_path not in changed:
            notes.append(
                f"dropped finding for {finding.file_path}: not among the diff's changed files"
            )
            continue
        if finding.line_start is not None:
            known_lines = added_by_path.get(finding.file_path)
            if known_lines is not None and finding.line_start not in known_lines:
                notes.append(
                    f"{finding.file_path}:{finding.line_start} is not an added line; "
                    f"kept the finding without a line anchor"
                )
                finding = finding.model_copy(update={"line_start": None, "line_end": None})
        kept.append(finding.model_copy(update={"agent_type": agent_type}))

    return kept, notes


class BaseAgent:
    """One specialist concern, grounded, bounded, and recorded."""

    agent_type: ClassVar[AgentType]

    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        router: ModelRouter | None = None,
        prompts: PromptRegistry | None = None,
        memory: CodeMemory | None = None,
        budget: BudgetGuard | None = None,
        settings: Settings | None = None,
        scope: CapabilityScope | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm or get_llm_client(self.settings)
        self.router = router or get_model_router()
        self.prompts = prompts or get_prompt_registry()
        self.memory = memory
        self.budget = budget
        self.scope = scope or scope_for(self.agent_type)

    # --- prompt ---------------------------------------------------------------
    def build_user_prompt(self, inp: SpecialistInput) -> str:
        lines: list[str] = [
            f"REPO: {inp.repo}",
            f"PR: #{inp.pr_number} ({inp.head_sha[:12]})",
        ]
        if inp.title:
            lines.append(f"TITLE: {inp.title}")

        lines.append("CHANGED_FILES:")
        if inp.changed_files:
            for changed in inp.changed_files:
                stats = f"{changed.status}, +{changed.additions} -{changed.deletions}"
                lines.append(f"- {changed.path} ({stats})")
        else:
            lines.append("- (not supplied)")

        diff = inp.diff
        truncated = False
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS]
            truncated = True
        lines.append("DIFF:")
        lines.append(diff)
        if truncated:
            lines.append("(diff truncated)")

        lines.append("RETRIEVED_CONTEXT:")
        if inp.context:
            budget = MAX_CONTEXT_CHARS
            for chunk in inp.context:
                rendered = chunk.as_prompt_line()
                if budget - len(rendered) < 0:
                    lines.append("(context truncated)")
                    break
                lines.append(rendered)
                budget -= len(rendered)
        else:
            lines.append("(none retrieved for this repository)")

        lines.append("INSTRUCTIONS:")
        lines.append(
            "- Report only issues on lines added in this diff. "
            "If the context does not support a claim, do not make it."
        )
        # Everything above this line is attacker-influencable, so the prompt ends by
        # saying so. The guard (see `security/injection_guard.py`) is the mechanical
        # half of that defense; this is the half that tells the model how to read it.
        lines.append(UNTRUSTED_INPUT_CLAUSE)
        return "\n".join(lines)

    # --- run ------------------------------------------------------------------
    def refine(self, findings: list[Finding], inp: SpecialistInput) -> list[Finding]:
        """Domain-specific adjustment, applied after anchoring and before the result
        is returned. Default: unchanged.

        This exists because one specialist genuinely needs it -- the tests pass caps
        confidence on coverage claims it cannot verify -- and because the audit trail
        is better served by an explicit hook than by a subclass quietly overwriting
        run().
        """
        return findings

    async def run(self, inp: SpecialistInput) -> SpecialistResult:
        model = self.router.model_for(self.agent_type)
        result = SpecialistResult(agent_type=self.agent_type, model=model)
        started = time.perf_counter()

        async with Span(
            str(self.agent_type),
            inp.review_id,
            note=f"grounded {self.agent_type.value} pass",
            payload={"scope": self.scope.name, "model": model},
        ) as active:
            # 1. budget, before any spend
            if self.budget is not None:
                status = await self.budget.status()
                if not status.allowed:
                    result.errors.append(f"budget blocked this pass: {status.reason}")
                    result.truncated = True
                    active.outcome = Outcome.INCOMPLETE.value
                    await active.event(
                        EventType.DECISION,
                        outcome=Outcome.INCOMPLETE.value,
                        payload={"reason": "budget", "spent_today": status.spent_today},
                    )
                    result.duration_ms = int((time.perf_counter() - started) * 1000)
                    return result

            # 2. retrieval, if the orchestrator did not already supply context
            context = inp.context
            if not context and self.memory is not None:
                context = await self._retrieve(inp, active)
            elif context:
                await active.event(
                    EventType.TOOL_CALL,
                    payload={
                        "tool": "retrieve_code_context",
                        "chunks": len(context),
                        "paths": _unique_paths(context)[:20],
                    },
                )

            # 3. the guard, then the prompt. Redaction replaces credential-shaped
            #    literals with a marker (the value never reaches the provider) and
            #    instruction-shaped spans are recorded rather than deleted, so a
            #    hostile comment is still visible to the human who approves the review.
            guarded_diff, context, report = guard_payload(inp.diff, context)
            if report.touched:
                await active.event(EventType.DECISION, payload=report.as_event_payload())
            result.errors.extend(report.warnings())

            # One effective input carries the guarded diff and context so the prompt,
            # the anchoring check and refine() all see the same thing. Line numbers are
            # unchanged by redaction, so anchoring is unaffected.
            effective = SpecialistInput(
                review_id=inp.review_id,
                repo=inp.repo,
                pr_number=inp.pr_number,
                head_sha=inp.head_sha,
                diff=guarded_diff,
                changed_files=inp.changed_files,
                title=inp.title,
                context=context,
            )
            template = self.prompts.get(self.agent_type)
            user_prompt = self.build_user_prompt(effective)

            # 4. the bounded, retried model call
            call_started = time.perf_counter()
            try:
                payload, response = await call_with_timeout(
                    lambda: self.llm.complete_structured(
                        FindingsPayload,
                        model=model,
                        system=template.system,
                        user=user_prompt,
                        max_tokens=self.router.max_tokens_for(self.agent_type),
                    ),
                    self.settings.agent_timeout_seconds,
                    label=f"{self.agent_type.value} llm pass",
                )
            except (TimeoutExceeded, CircuitOpenError) as exc:
                result.errors.append(f"{type(exc).__name__}: {exc}")
                result.truncated = True
                active.outcome = Outcome.INCOMPLETE.value
                await active.event(
                    EventType.LLM_CALL,
                    model=model,
                    latency_ms=int((time.perf_counter() - call_started) * 1000),
                    outcome=Outcome.ERROR.value,
                    payload={"error": str(exc), "prompt_hash": template.sha256},
                )
                result.duration_ms = int((time.perf_counter() - started) * 1000)
                return result
            except Exception as exc:  # noqa: BLE001 - a failed pass must not fail the review
                result.errors.append(f"llm failure: {exc}")
                result.truncated = True
                active.outcome = Outcome.INCOMPLETE.value
                await active.event(
                    EventType.LLM_CALL,
                    model=model,
                    latency_ms=int((time.perf_counter() - call_started) * 1000),
                    outcome=Outcome.ERROR.value,
                    payload={"error": str(exc), "prompt_hash": template.sha256},
                )
                result.duration_ms = int((time.perf_counter() - started) * 1000)
                return result

            call_latency_ms = int((time.perf_counter() - call_started) * 1000)

            # 5. grounding check, normalization, then the domain hook
            findings, notes = anchor_findings(
                payload.findings, effective, agent_type=self.agent_type
            )
            findings = self.refine(findings, effective)

            # 6. the ledger entry for this call
            await active.event(
                EventType.LLM_CALL,
                model=response.model,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
                latency_ms=call_latency_ms,
                outcome=Outcome.OK.value,
                confidence=max((f.confidence for f in findings), default=None),
                payload={
                    "prompt_version": template.version,
                    "prompt_hash": template.sha256,
                    "retrieved_chunks": len(context),
                    "raw_findings": len(payload.findings),
                    "kept_findings": len(findings),
                    "dropped": notes[:10],
                    "simulated": response.simulated,
                },
            )

            result.findings = findings
            result.errors.extend(notes)
            result.model = response.model
            result.prompt_version = template.version
            result.prompt_hash = template.sha256
            result.tokens_in = response.tokens_in
            result.tokens_out = response.tokens_out
            result.cost_usd = response.cost_usd
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            active.outcome = Outcome.OK.value
            active.confidence = max((f.confidence for f in findings), default=None)
            return result

    async def _retrieve(self, inp: SpecialistInput, active: Span) -> list[RetrievedChunk]:
        assert self.memory is not None  # noqa: S101 - guarded by the caller
        try:
            chunks = await self.memory.search(inp.repo, self._retrieval_query(inp))
        except Exception as exc:  # noqa: BLE001 - retrieval failure degrades grounding
            await active.event(
                EventType.TOOL_CALL,
                outcome=Outcome.ERROR.value,
                payload={"tool": "retrieve_code_context", "error": str(exc)},
            )
            return []
        await active.event(
            EventType.TOOL_CALL,
            payload={
                "tool": "retrieve_code_context",
                "chunks": len(chunks),
                "paths": _unique_paths(chunks)[:20],
            },
        )
        return chunks

    @staticmethod
    def _retrieval_query(inp: SpecialistInput) -> str:
        """Query on the added lines, not the whole diff: deleted code and hunk
        headers carry little retrieval signal and widen the query needlessly."""
        added = parse_added_lines(inp.diff)
        body = "\n".join(line.text for line in added[:300]) if added else inp.diff[:4000]
        paths = " ".join(f.path for f in inp.changed_files[:10])
        return f"{paths}\n{body}".strip()[:4000]


def _unique_paths(chunks: list[RetrievedChunk]) -> list[str]:
    seen: list[str] = []
    for chunk in chunks:
        if chunk.path not in seen:
            seen.append(chunk.path)
    return seen
