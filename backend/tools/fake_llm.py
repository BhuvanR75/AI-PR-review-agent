"""The rules the offline reviewer applies.

This is not a mock that returns fixed strings. It reads the diff, finds real added
lines, and reports them with real new-file line numbers, so the whole pipeline --
aggregator, dedup, HITL gate, GitHub comment anchoring -- is exercised against
plausible findings instead of placeholder ones.

It is honest about being a heuristic: it catches what a regex can catch, by design.
Its job is to make the spine runnable and testable with no API key, not to compete
with the model that replaces it when LLM_PROVIDER=openai.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from backend.models.enums import AgentType, Category, Severity
from backend.tools.diffparse import AddedLine, is_test_path, parse_added_lines, touched_tests


@dataclass(frozen=True)
class Rule:
    agent: AgentType
    pattern: re.Pattern[str]
    category: Category
    severity: Severity
    confidence: float
    summary: str
    suggestion: str
    rationale: str
    exclude_tests: bool = False
    require_path_substring: str | None = None


def _rule(
    agent: AgentType,
    pattern: str,
    category: Category,
    severity: Severity,
    confidence: float,
    summary: str,
    suggestion: str,
    rationale: str,
    **kwargs: object,
) -> Rule:
    return Rule(
        agent=agent,
        pattern=re.compile(pattern),
        category=category,
        severity=severity,
        confidence=confidence,
        summary=summary,
        suggestion=suggestion,
        rationale=rationale,
        **kwargs,  # type: ignore[arg-type]
    )


RULES: tuple[Rule, ...] = (
    # Each rationale states evidence (the code shape), mechanism (how input reaches
    # the harm), and consequence (what becomes possible). The matched line text is
    # appended at emission time, so the prose below must read naturally before it.
    _rule(
        AgentType.SECURITY,
        r"\.execute\s*\(\s*f?[\"']|\.execute\s*\([^)]*%\s*\(",
        Category.INJECTION,
        Severity.CRITICAL,
        0.86,
        "SQL assembled with string interpolation is passed to .execute()",
        "Use a parameterized query: keep placeholders in the SQL text and pass values "
        "separately, e.g. cursor.execute(\"... WHERE id = %s\", (user_id,)).",
        "The statement reaches .execute() built by string formatting, so any value "
        "interpolated into it is parsed as SQL syntax rather than carried as data. If "
        "that value is ever caller-supplied, the query becomes a direct injection "
        "path: arbitrary reads, writes, or deletes against this database. Even if the "
        "current callers pass constants, the pattern is one refactor away from "
        "accepting user input, and nothing at the call site would flag the change.",
    ),
    _rule(
        AgentType.SECURITY,
        r"subprocess\.(?:run|call|check_output|Popen)\([^)]*shell\s*=\s*True",
        Category.INJECTION,
        Severity.HIGH,
        0.82,
        "subprocess call executes a command string through a shell",
        "Drop shell=True and pass an argument list (e.g. subprocess.run(['git', "
        "'status'], check=True)); shell-quote only if a shell is genuinely required.",
        "With shell=True the command string is interpreted by /bin/sh, so shell "
        "metacharacters in any interpolated portion -- ;, &&, $(...), backticks -- "
        "execute as additional commands. A filename, branch name, or user field that "
        "flows into this string turns this call into arbitrary command execution on "
        "the host running the review agent.",
    ),
    _rule(
        AgentType.SECURITY,
        r"\bos\.system\s*\(|\beval\s*\(|\bexec\s*\(",
        Category.INJECTION,
        Severity.HIGH,
        0.78,
        "Dynamically constructed string is evaluated or executed",
        "Replace with an explicit dispatch table or a direct library call; if the "
        "string must vary, parse it into a known set of operations first.",
        "The flagged line evaluates code at runtime, so the contents of the string "
        "become instructions rather than data. Any attacker-influenced part of that "
        "string yields arbitrary code execution inside this process, and even a "
        "trusted-only path makes the code impossible to analyze statically or test "
        "exhaustively.",
    ),
    _rule(
        AgentType.SECURITY,
        # Case-insensitive: `STRIPE_API_KEY = "..."` is the shape this actually takes.
        r"(?i)(?:password|passwd|secret|api_key|apikey|access_token|private_key)\s*[:=]\s*[\"'][^\"']{7,}[\"']",
        Category.SECRETS,
        Severity.CRITICAL,
        0.9,
        "Credential-shaped literal committed in source",
        "Move the value to an environment variable or a secret manager and read it at "
        "startup, then rotate the exposed credential -- anything committed is assumed "
        "compromised even after removal.",
        "A long literal is assigned to a credential-named variable, which reads as a "
        "live secret rather than a placeholder. Once pushed, the value is in git "
        "history forever and visible to everyone with repo access, so it should be "
        "treated as compromised: rotate it first, then switch this call site to read "
        "the credential from the environment.",
        exclude_tests=True,
    ),
    _rule(
        AgentType.SECURITY,
        r"pickle\.loads?\s*\(|yaml\.load\s*\((?![^)]*Safe)",
        Category.DESERIALIZATION,
        Severity.HIGH,
        0.8,
        "Untrusted data is deserialized with pickle / unsafe yaml.load",
        "Use JSON or yaml.safe_load instead; if pickle is required, restrict it to "
        "byte streams this process wrote and verify integrity before loading.",
        "Deserializing with pickle or yaml.load executes object construction "
        "attacker-controlled: a crafted payload runs arbitrary code during the load "
        "call itself, before any validation in this function can run. Anything that "
        "reaches this line from a network response, a message queue, or user upload "
        "is a remote code execution vector.",
    ),
    _rule(
        AgentType.SECURITY,
        r"verify\s*=\s*False|ssl_context\s*=\s*None|InsecureSkipVerify",
        Category.AUTH,
        Severity.MEDIUM,
        0.75,
        "TLS certificate verification disabled on this connection",
        "Leave verification on; for a self-signed or internal certificate, add it to "
        "the trust store or pin it rather than disabling verification.",
        "The request is configured to accept any certificate, so a machine-in-the-"
        "middle on the network path can impersonate the server, read everything this "
        "call sends -- including any credentials -- and alter the response "
        "undetected. This silently downgrades every caller of this code path, not "
        "just the one that motivated the change.",
    ),
    _rule(
        AgentType.QUALITY,
        r"^\s*except\s*:\s*$|except\s*\(?\s*BaseException",
        Category.CORRECTNESS,
        Severity.MEDIUM,
        0.8,
        "Bare except catches everything, including KeyboardInterrupt and SystemExit",
        "Catch the specific exception types this block can actually handle and let "
        "the rest propagate; at minimum catch Exception instead of everything.",
        "A bare except intercepts every exception, including the ones that exist to "
        "stop the process (KeyboardInterrupt, SystemExit). Failures inside the try "
        "body vanish here and reappear later as a confusing secondary error far from "
        "the real cause -- or the program simply ignores a shutdown signal. Catching "
        "the specific types keeps the recovery intentional and debuggable.",
    ),
    _rule(
        AgentType.QUALITY,
        r"==\s*None|!=\s*None",
        Category.LOGIC,
        Severity.LOW,
        0.72,
        "None comparison uses equality instead of identity",
        "Use `is None` / `is not None`.",
        "`==` calls __eq__, which a class is free to override or to return a non-bool "
        "for, so this test can be fooled by an object whose equality is degenerate or "
        "by a badly implemented __eq__ returning False for None. Identity via `is` is "
        "the language-defined way to test for None and cannot be overridden.",
    ),
    _rule(
        AgentType.QUALITY,
        r"def\s+\w+\s*\([^)]*=\s*(\[\]|\{\})\s*[,)]",
        Category.CORRECTNESS,
        Severity.MEDIUM,
        0.84,
        "Mutable default argument is created once and shared across all calls",
        "Default to None and create the container inside the function body: "
        "`def f(items=None): items = items if items is not None else []`.",
        "The default list/dict is evaluated once at function definition, so every "
        "call that relies on the default mutates the same object. A caller who appends "
        "on the first invocation changes what the second caller receives -- state "
        "leaks across unrelated calls and the bug only reproduces when the function "
        "is called more than once in a process.",
    ),
    _rule(
        AgentType.QUALITY,
        r"\btime\.sleep\s*\(\s*\d",
        Category.CORRECTNESS,
        Severity.LOW,
        0.65,
        "Fixed sleep stands in for waiting on an actual condition",
        "Wait on the real condition: poll with a timeout that checks the state, use an "
        "Event/Future, or await the task -- instead of sleeping a constant duration.",
        "A constant sleep encodes a guess about how long the other side takes: under "
        "load it is too short and the code proceeds before the condition holds (a "
        "race), while on a fast machine it is too long and every call pays the delay. "
        "Waiting on the condition itself is both correct under load and faster when "
        "the condition is already met.",
    ),
    _rule(
        AgentType.QUALITY,
        r"except\s+\w*Error[^:]*:\s*pass\s*$",
        Category.CORRECTNESS,
        Severity.MEDIUM,
        0.7,
        "Exception is swallowed silently with `pass`",
        "Log the failure with enough context to diagnose it (logger.exception or a "
        "structured event), or handle only the cases that are genuinely expected.",
        "The except block discards the exception without a record, so when this code "
        "path misbehaves in production there is no trace of why: the operation "
        "appears to succeed or silently skips work. At minimum, log the exception; if "
        "the error is genuinely expected, say so in a comment and narrow the caught "
        "type.",
    ),
    _rule(
        AgentType.TESTS,
        r"assert\s+True\b|assert\s+1\s*==\s*1",
        Category.BRITTLE_TEST,
        Severity.MEDIUM,
        0.88,
        "Assertion cannot fail, so the test verifies nothing",
        "Assert the value or behaviour the test exists to pin down (the returned "
        "result, the raised error, the observed state) instead of a tautology.",
        "This assertion is true regardless of what the code under test does, so the "
        "test passes after a regression exactly as it did before. It gives the suite "
        "the appearance of covering this path while actually covering nothing -- the "
        "most misleading kind of green.",
    ),
    _rule(
        AgentType.TESTS,
        r"\btime\.sleep\s*\(",
        Category.BRITTLE_TEST,
        Severity.LOW,
        0.66,
        "Test synchronizes with time.sleep, making it timing-dependent",
        "Wait for the actual condition with a deadline (poll the state, await the "
        "event, or use a test utility that waits), or fake the clock for "
        "time-dependent logic.",
        "The test sleeps a fixed duration and then assumes the work finished. On a "
        "loaded CI machine the assumption breaks and the test fails intermittently -- "
        "the most expensive kind of failure to trust. Waiting on the real condition "
        "makes the test both faster (no minimum duration) and deterministic.",
    ),
    _rule(
        AgentType.TESTS,
        r"@pytest\.mark\.skip(?!\()|@unittest\.skip\b",
        Category.COVERAGE_GAP,
        Severity.LOW,
        0.7,
        "Test skipped with no recorded reason",
        "Add a reason= and a tracking issue so the skip can be triaged, or delete the "
        "test if the behaviour is gone.",
        "A bare skip removes this case from the suite with no explanation of when it "
        "can return. Unowned skips are almost never revisited: the coverage they "
        "promised quietly disappears while the file still looks complete.",
    ),
    _rule(
        AgentType.DOCS,
        r"^\s*(?:async\s+)?def\s+(?!_)\w+\s*\([^)]*\)\s*(?:->[^:]+)?:\s*$",
        Category.MISSING_DOCS,
        Severity.LOW,
        0.62,
        "Public function added with no docstring",
        "Add a short docstring stating the contract: what it returns, what it "
        "requires of its inputs, and any side effect a caller would not expect.",
        "The function is new and public, and its contract exists only in its body -- "
        "every caller must read the implementation to learn what it does and what it "
        "assumes. A docstring stating the contract (not the mechanics) is cheaper "
        "than that reconstruction and prevents the misuse that misreadings cause.",
        require_path_substring=".py",
    ),
    _rule(
        AgentType.DOCS,
        r"^\s*class\s+\w+\s*(?:\([^)]*\))?:\s*$",
        Category.MISSING_DOCS,
        Severity.INFO,
        0.55,
        "Public class added with no docstring",
        "Add a docstring naming the single responsibility of the class and its key "
        "invariant.",
        "The class states its responsibility nowhere, so its intended use and "
        "invariants are inferred from the method list -- which invites stuffing "
        "unrelated behaviour into it as the code grows. One sentence on what it is "
        "responsible for (and what it is not) anchors future changes.",
        require_path_substring=".py",
    ),
    _rule(
        AgentType.DOCS,
        r"\b(?:TODO|FIXME|XXX)\b(?!.*(?:#\d+|issue|ISSUE))",
        Category.OUTDATED_DOCS,
        Severity.INFO,
        0.6,
        "TODO committed without a tracking reference",
        "Reference an issue number (or a link) in the TODO so it can be found, "
        "triaged, and closed.",
        "The marker records an intention with no owner and no tracker entry; search "
        "is the only way anyone will ever find it again. An issue link turns it into "
        "work that can be prioritized -- without one, the TODO usually outlives the "
        "context that created it.",
    ),
)





def _windows(added: list[AddedLine], span: int = 3) -> list[tuple[AddedLine, list[AddedLine]]]:
    r"""Each added line paired with the following consecutive added lines.

    Two views are needed per line. A call broken across lines (`cursor.execute(` on one
    line, its argument on the next) is only visible across a window, while an anchored
    rule like ``^\s*except\s*:$`` only ever matches a line by itself. So both views are
    tried and, when the window matches, the line the match *starts* on is the one
    reported -- otherwise a finding about `cursor.execute(` would be blamed on the
    unrelated assignment one line above it.
    """
    windows: list[tuple[AddedLine, list[AddedLine]]] = []
    for index, line in enumerate(added):
        group = [line]
        previous = line
        for following in added[index + 1 : index + span]:
            if following.path != line.path or following.line_no != previous.line_no + 1:
                break
            group.append(following)
            previous = following
        windows.append((line, group))
    return windows


def _matched_line(rule: Rule, line: AddedLine, group: list[AddedLine]) -> tuple[int, str] | None:
    """The offending line number plus the code to quote as evidence, or None.

    A direct line match quotes that line. A window match (the offense spans lines,
    like `cursor.execute(` with its argument below) quotes from the line the match
    starts on through the rest of the window, so the evidence shows the actual
    offending span rather than whatever unrelated statement happens to anchor it.
    """
    if rule.exclude_tests and is_test_path(line.path):
        return None
    if rule.require_path_substring and rule.require_path_substring not in line.path:
        return None
    if rule.pattern.search(line.text):
        return line.line_no, line.stripped

    joined = "\n".join(candidate.text for candidate in group)
    match = rule.pattern.search(joined)
    if match is None:
        return None
    offset = joined[: match.start()].count("\n")
    start = min(offset, len(group) - 1)
    evidence = " ".join(candidate.stripped for candidate in group[start:])
    return group[start].line_no, evidence[:200]


def review_diff(diff: str, agent: AgentType) -> list[dict[str, Any]]:
    """Apply the rules for one specialist to a diff. Returns Finding-shaped dicts."""
    added = parse_added_lines(diff)
    findings: list[dict[str, Any]] = []

    for line, group in _windows(added):
        for rule in RULES:
            if rule.agent is not agent:
                continue
            hit = _matched_line(rule, line, group)
            if hit is None:
                continue
            matched, evidence = hit
            findings.append(
                {
                    "agent_type": str(agent),
                    "severity": str(rule.severity),
                    "category": str(rule.category),
                    "summary": rule.summary,
                    "file_path": line.path,
                    "line_start": matched,
                    "line_end": matched,
                    "suggestion": rule.suggestion,
                    "confidence": rule.confidence,
                    # Evidence first (the exact code, quoted), then the rule's
                    # mechanism-and-consequence prose. The quote is what makes the
                    # finding checkable: the author can see the flagged span in place.
                    "rationale": (
                        f"On line {matched} of {line.path}, the added code is: "
                        f"`{evidence[:160]}` {rule.rationale}"
                    ),
                }
            )

    # The structural coverage claim only holds when no test file was touched at all.
    # Without this guard the pass reports "no test change" on a diff that adds tests.
    if agent is AgentType.TESTS and findings == [] and added and not touched_tests(diff):
        findings.append(_missing_test_finding(diff))

    return _dedupe_by_location(findings)


def _missing_test_finding(diff: str) -> dict[str, Any]:
    """The tests specialist's structural rule: source changed, no test touched."""
    from backend.tools.diffparse import changed_paths

    paths = [p for p in changed_paths(diff) if not is_test_path(p)]
    anchor = paths[0] if paths else "."
    return {
        "agent_type": str(AgentType.TESTS),
        "severity": str(Severity.MEDIUM),
        "category": str(Category.MISSING_TEST),
        "summary": f"No test change accompanies {len(paths)} modified source file(s)",
        "file_path": anchor,
        "line_start": None,
        "line_end": None,
        "suggestion": (
            "Add a test that fails before this change and passes after it: arrange the "
            f"state around {anchor}, call the changed behaviour with a representative "
            "input (include one edge case), and assert the exact expected outcome."
        ),
        "confidence": 0.6,
        "rationale": (
            "The diff modifies "
            + (", ".join(paths[:3]) if paths else "source files")
            + (f" and {len(paths) - 3} more" if len(paths) > 3 else "")
            + " without touching any test path, so the new behaviour is unverified by "
            "the change itself. If the logic regressed tomorrow -- a swapped argument, "
            "an inverted condition -- the entire suite would stay green, and the bug "
            "would surface in production rather than in CI. Naming one representative "
            "input and its expected output closes that gap for the change's main path."
        ),
    }


def _dedupe_by_location(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One finding per (path, line, category) -- the same key the aggregator uses."""
    best: dict[tuple[str, int, str], dict[str, Any]] = {}
    for finding in findings:
        key = (
            str(finding["file_path"]),
            int(finding["line_start"]) if finding["line_start"] else 0,
            str(finding["category"]),
        )
        existing = best.get(key)
        if existing is None or float(finding["confidence"]) > float(existing["confidence"]):
            best[key] = finding
    return sorted(
        best.values(),
        key=lambda f: (str(f["file_path"]), int(f["line_start"]) if f["line_start"] else 0),
    )


def agent_for_marker(marker: str) -> AgentType | None:
    for agent in AgentType:
        if agent.value == marker:
            return agent
    return None


def is_test_change(diff: str) -> bool:
    return touched_tests(diff)
