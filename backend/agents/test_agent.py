"""Test-coverage pass. Asks one question: what is now unverified?

This is the pass most likely to be wrong in the quiet direction -- it can only see the
diff, so it cannot know that coverage exists in a test file the PR did not touch. The
prompt is written to make it admit that, and its findings carry a deliberately lower
default confidence than the security pass for exactly this reason.
"""

from __future__ import annotations

from typing import ClassVar

from backend.agents.base_agent import BaseAgent, SpecialistInput
from backend.models.enums import AgentType
from backend.models.findings import Finding
from backend.tools.diffparse import touched_tests

#: A missing-test finding with no line anchor is only useful if the diff shows no test
#: change at all; otherwise the pass is guessing about files it cannot see.
LOW_CONFIDENCE_WHEN_TESTS_PRESENT = 0.35


class TestsAgent(BaseAgent):
    agent_type: ClassVar[AgentType] = AgentType.TESTS

    @property
    def concern(self) -> str:
        return "missing cases, tautological assertions, timing-dependent tests"

    def refine(self, findings: list[Finding], inp: SpecialistInput) -> list[Finding]:
        """Cap confidence on line-less coverage claims when the PR did touch tests.

        The base anchoring step cannot express this: the claim is legitimate, just
        weakly supported. Lowering confidence (rather than dropping the finding) keeps
        the aggregator's confidence-weighted gate honest, and keeps a human in the loop
        for a case the agent cannot verify.
        """
        if not touched_tests(inp.diff):
            return findings
        refined: list[Finding] = []
        for finding in findings:
            if (
                finding.line_start is None
                and finding.confidence > LOW_CONFIDENCE_WHEN_TESTS_PRESENT
            ):
                refined.append(
                    finding.model_copy(
                        update={
                            "confidence": LOW_CONFIDENCE_WHEN_TESTS_PRESENT,
                            "rationale": finding.rationale
                            + " (Confidence capped: this PR does touch test files, which this"
                            " pass cannot fully see.)",
                        }
                    )
                )
            else:
                refined.append(finding)
        return refined
