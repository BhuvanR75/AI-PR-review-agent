"""The Finding contract -- the object that travels between every component (L2).

Everything downstream (the aggregator, the human gate, the audit trail, the GitHub
poster) is a function of this shape, so the validators here are load-bearing rather
than cosmetic: a Finding that cannot name a location cannot be posted inline, and a
Finding without a rationale cannot be audited or disputed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.models.enums import AgentType, Category, Severity


class Finding(BaseModel):
    """One raised concern, tied to a location, with evidence and confidence."""

    agent_type: AgentType = Field(description="Which specialist pass raised this.")
    severity: Severity
    category: Category
    summary: str = Field(min_length=1, max_length=400)
    file_path: str = Field(min_length=1, description="Repo-relative path, as it appears in the PR.")
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)
    suggestion: str | None = Field(default=None, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(
        min_length=1,
        description="Why this was raised. Required: a finding without evidence is not auditable.",
    )
    agreed_by: list[AgentType] = Field(
        default_factory=list,
        description="Other specialists that raised the same issue (set by the aggregator).",
    )
    incomplete: bool = Field(
        default=False,
        description="True when the agent timed out or errored: degrades to slower-but-correct.",
    )

    @field_validator("file_path")
    @classmethod
    def _normalize_path(cls, value: str) -> str:
        # The GitHub API and diffs disagree about the leading "./"; normalize once here
        # so dedup and posting agree later.
        cleaned = value.strip().lstrip("./")
        if not cleaned:
            raise ValueError("file_path must not be empty")
        return cleaned

    @model_validator(mode="after")
    def _check_line_range(self) -> Finding:
        if (
            self.line_start is not None
            and self.line_end is not None
            and self.line_end < self.line_start
        ):
            raise ValueError("line_end must be >= line_start")
        if self.line_end is not None and self.line_start is None:
            raise ValueError("line_end requires line_start")
        return self

    @property
    def dedup_key(self) -> str:
        """Cross-agent dedup key. Deliberately coarse: same file, same anchor line,
        same category. The aggregator logs near-misses separately rather than
        guessing at equivalence."""
        anchor = self.line_start if self.line_start is not None else 0
        return f"{self.file_path}:{anchor}:{self.category}"

    @property
    def locator(self) -> str:
        if self.line_start is None:
            return self.file_path
        if self.line_end and self.line_end != self.line_start:
            return f"{self.file_path}:{self.line_start}-{self.line_end}"
        return f"{self.file_path}:{self.line_start}"


class SpecialistResult(BaseModel):
    """What one specialist pass hands back to the orchestrator."""

    agent_type: AgentType
    findings: list[Finding] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    prompt_hash: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    truncated: bool = Field(
        default=False, description="True when the pass gave up (timeout) and results are partial."
    )


class FindingsPayload(BaseModel):
    """The JSON schema the LLM is asked to emit, per specialist."""

    findings: list[Finding] = Field(default_factory=list)
