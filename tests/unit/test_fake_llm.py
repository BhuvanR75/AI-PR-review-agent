"""The offline reviewer is the default provider, so it is tested like production code.

What matters is not that it finds *something*, but that what it finds is anchored to a
line that exists on the new side of the diff. A heuristic that reports a plausible
finding at a wrong line would make the whole pipeline look correct while producing
comments GitHub silently rejects.
"""

from __future__ import annotations

from backend.integrations.demo_pr import DEMO_DIFF
from backend.models.enums import AgentType, Category, Severity
from backend.tools.diffparse import parse_added_lines
from backend.tools.fake_llm import review_diff


def categories(agent: AgentType, diff: str = DEMO_DIFF) -> set[Category]:
    return {
        Category(f["category"])
        for f in review_diff(diff, agent)  # type: ignore[arg-type]
    }


def test_security_pass_catches_the_interpolated_query() -> None:
    findings = review_diff(DEMO_DIFF, AgentType.SECURITY)
    injections = [f for f in findings if f["category"] == Category.INJECTION]
    assert injections, "the f-string reaching .execute() must be reported"
    assert injections[0]["severity"] == Severity.CRITICAL


def test_security_pass_catches_the_committed_credential() -> None:
    findings = review_diff(DEMO_DIFF, AgentType.SECURITY)
    secrets = [f for f in findings if f["category"] == Category.SECRETS]
    assert secrets
    assert secrets[0]["file_path"] == "billing/stripe.py"


def test_multiline_call_is_still_seen() -> None:
    """`cursor.execute(` and its argument are on different lines in the demo diff, so
    this only passes because rules are evaluated over a lookahead window."""
    findings = review_diff(DEMO_DIFF, AgentType.SECURITY)
    injection = next(f for f in findings if f["category"] == Category.INJECTION)
    line_text = next(
        line.text
        for line in parse_added_lines(DEMO_DIFF)
        if line.line_no == injection["line_start"] and line.path == injection["file_path"]
    )
    assert "execute" in line_text


def test_quality_pass_catches_bare_except_and_none_comparison() -> None:
    found = categories(AgentType.QUALITY)
    assert Category.CORRECTNESS in found  # bare except
    assert Category.LOGIC in found  # == None


def test_tests_pass_reports_a_missing_test_when_source_changed() -> None:
    findings = review_diff(DEMO_DIFF, AgentType.TESTS)
    assert any(f["category"] == Category.MISSING_TEST for f in findings)
    missing = next(f for f in findings if f["category"] == Category.MISSING_TEST)
    assert missing["line_start"] is None, "a coverage gap has no specific line"
    assert missing["file_path"] == "billing/stripe.py"


def test_tests_pass_stays_quiet_when_a_test_file_changed() -> None:
    diff = DEMO_DIFF + (
        "\ndiff --git a/tests/test_billing.py b/tests/test_billing.py\n"
        "--- a/tests/test_billing.py\n"
        "+++ b/tests/test_billing.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+def test_refund():\n"
        "+    assert refund_customer('x') is None\n"
    )
    findings = review_diff(diff, AgentType.TESTS)
    assert not any(f["category"] == Category.MISSING_TEST for f in findings)


def test_docs_pass_reports_undocumented_public_functions() -> None:
    findings = review_diff(DEMO_DIFF, AgentType.DOCS)
    assert findings, "the new public functions have no docstrings"
    assert {f["category"] for f in findings} == {Category.MISSING_DOCS}
    assert {f["file_path"] for f in findings} == {"billing/stripe.py", "billing/formatting.py"}


def test_only_the_agents_own_rules_run() -> None:
    """Each pass is one concern; a security finding must not appear in the docs pass."""
    docs_agents = {
        f["agent_type"] for f in review_diff(DEMO_DIFF, AgentType.DOCS)  # type: ignore[arg-type]
    }
    assert docs_agents == {"docs"}


def test_every_finding_lands_on_an_added_line() -> None:
    """The invariant the whole pipeline relies on for inline anchoring."""
    added = {(line.path, line.line_no) for line in parse_added_lines(DEMO_DIFF)}
    for agent in AgentType:
        if agent is AgentType.AGGREGATOR:
            continue
        for finding in review_diff(DEMO_DIFF, agent):
            if finding["line_start"] is None:
                continue
            assert (finding["file_path"], finding["line_start"]) in added, finding


def test_findings_are_deduplicated_per_location_and_category() -> None:
    findings = review_diff(DEMO_DIFF, AgentType.SECURITY)
    keys = [
        (f["file_path"], f["line_start"], f["category"]) for f in findings
    ]
    assert len(keys) == len(set(keys))


def test_empty_diff_produces_nothing() -> None:
    for agent in (AgentType.SECURITY, AgentType.QUALITY, AgentType.TESTS, AgentType.DOCS):
        assert review_diff("", agent) == []


def test_clean_change_produces_no_findings() -> None:
    clean = (
        "diff --git a/util.py b/util.py\n"
        "--- a/util.py\n"
        "+++ b/util.py\n"
        "@@ -1,2 +1,3 @@\n"
        " def add(a, b):\n"
        '     """Add two numbers."""\n'
        "+    return a + b\n"
    )
    findings = review_diff(clean, AgentType.QUALITY)
    assert findings == []
