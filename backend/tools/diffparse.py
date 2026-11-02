"""Unified diff parsing.

Line anchoring is the most common source of silent posting failures: a finding has to
name a line that exists on the *new* side of the diff, or GitHub rejects the comment.
So added lines are tracked with their new-file numbers, and everything that reports a
location goes through here rather than guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>\S+) b/(?P<b>\S+)")

TEST_PATH_MARKERS = ("test_", "_test.", ".test.", ".spec.", "/tests/", "/test/")


@dataclass(frozen=True)
class AddedLine:
    path: str
    line_no: int
    text: str

    @property
    def stripped(self) -> str:
        return self.text.strip()


@dataclass(frozen=True)
class DiffHunk:
    path: str
    old_start: int
    new_start: int
    lines: tuple[str, ...]


def is_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in TEST_PATH_MARKERS)


def parse_added_lines(diff: str) -> list[AddedLine]:
    """Every line added on the new side, with its new-file line number."""
    added: list[AddedLine] = []
    path = ""
    new_line = 0

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = "" if target == "/dev/null" else _strip_prefix(target)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git") or raw.startswith("index "):
            continue

        hunk = HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if not path:
            continue

        if raw.startswith("\\"):  # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            added.append(AddedLine(path=path, line_no=new_line, text=raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1
    return added


def _strip_prefix(target: str) -> str:
    for prefix in ("a/", "b/"):
        if target.startswith(prefix):
            return target[len(prefix) :]
    return target


def parse_hunks(diff: str) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    path = ""
    old_start = new_start = 0
    buffer: list[str] = []

    def flush() -> None:
        if buffer and path:
            hunks.append(
                DiffHunk(path=path, old_start=old_start, new_start=new_start, lines=tuple(buffer))
            )
        buffer.clear()

    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            flush()
            target = raw[4:].strip()
            path = "" if target == "/dev/null" else _strip_prefix(target)
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git") or raw.startswith("index "):
            continue
        hunk = HUNK_RE.match(raw)
        if hunk:
            flush()
            old_match = re.match(r"^@@ -(\d+)", raw)
            old_start = int(old_match.group(1)) if old_match else 0
            new_start = int(hunk.group(1))
            continue
        if path:
            buffer.append(raw)
    flush()
    return hunks


def changed_paths(diff: str) -> list[str]:
    paths: list[str] = []
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target != "/dev/null":
                candidate = _strip_prefix(target)
                if candidate not in paths:
                    paths.append(candidate)
    return paths


def touched_tests(diff: str) -> bool:
    return any(is_test_path(p) for p in changed_paths(diff))
