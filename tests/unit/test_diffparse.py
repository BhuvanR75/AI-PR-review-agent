"""Line anchoring is where silent posting failures come from, so the parser's new-file
line arithmetic is tested across multi-hunk and multi-file diffs."""

from __future__ import annotations

from backend.tools.diffparse import (
    changed_paths,
    is_test_path,
    parse_added_lines,
    parse_hunks,
    touched_tests,
)

SINGLE_FILE = """diff --git a/app.py b/app.py
index 111..222 100644
--- a/app.py
+++ b/app.py
@@ -1,4 +1,6 @@
 import os
-old_line
+new_first
+new_second
 context
 context2
"""

MULTI_HUNK = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 alpha
+first_added
 beta
 gamma
@@ -20,3 +21,4 @@
 delta
+second_added
 epsilon
 zeta
"""

MULTI_FILE = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,3 @@
 a
+b
 c
diff --git a/tests/test_app.py b/tests/test_app.py
--- a/tests/test_app.py
+++ b/tests/test_app.py
@@ -1,2 +1,3 @@
 x
+y
 z
"""


def test_added_lines_carry_new_file_numbers() -> None:
    added = parse_added_lines(SINGLE_FILE)
    assert [(a.path, a.line_no, a.stripped) for a in added] == [
        ("app.py", 2, "new_first"),
        ("app.py", 3, "new_second"),
    ]


def test_second_hunk_uses_its_own_start_line() -> None:
    added = parse_added_lines(MULTI_HUNK)
    assert [(a.line_no, a.stripped) for a in added] == [
        (2, "first_added"),
        (22, "second_added"),
    ]


def test_multiple_files_are_attributed_separately() -> None:
    added = parse_added_lines(MULTI_FILE)
    assert [(a.path, a.line_no) for a in added] == [
        ("app.py", 2),
        ("tests/test_app.py", 2),
    ]


def test_deleted_lines_do_not_advance_the_new_side() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1,4 +1,3 @@\n context\n-gone_one\n-gone_two\n+kept\n context2\n"
    )
    # context advances the new side to 2, the two deletions do not, so `kept` is line 2.
    added = parse_added_lines(diff)
    assert [(a.line_no, a.stripped) for a in added] == [(2, "kept")]


def test_new_file_has_no_prefix_to_strip() -> None:
    diff = (
        "diff --git a/new.py b/new.py\n--- /dev/null\n+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n+alpha\n+beta\n"
    )
    assert [a.line_no for a in parse_added_lines(diff)] == [1, 2]


def test_deleted_file_produces_no_added_lines() -> None:
    diff = "diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n"
    assert parse_added_lines(diff) == []


def test_no_newline_marker_is_ignored() -> None:
    diff = (
        "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
        "@@ -1 +1 @@\n-old\n+new\n\\ No newline at end of file\n"
    )
    assert [(a.line_no, a.stripped) for a in parse_added_lines(diff)] == [(1, "new")]


def test_changed_paths_lists_each_file_once() -> None:
    assert changed_paths(MULTI_FILE) == ["app.py", "tests/test_app.py"]


def test_test_path_detection() -> None:
    assert is_test_path("tests/test_app.py")
    assert is_test_path("src/app.test.ts")
    assert is_test_path("pkg/handler_test.go")
    assert not is_test_path("app.py")
    # "testing_helpers" is not "test_", so it is treated as source rather than a test.
    assert not is_test_path("src/testing_helpers/foo.py")


def test_touched_tests_flag() -> None:
    assert touched_tests(MULTI_FILE)
    assert not touched_tests(SINGLE_FILE)


def test_hunks_report_their_starts() -> None:
    hunks = parse_hunks(MULTI_HUNK)
    assert [(h.new_start, h.path) for h in hunks] == [(1, "app.py"), (21, "app.py")]
    # Hunks keep the diff's own prefixes, including the leading space on context lines.
    assert "+first_added" in hunks[0].lines
    assert " alpha" in hunks[0].lines


def test_empty_diff() -> None:
    assert parse_added_lines("") == []
    assert changed_paths("") == []
    assert parse_hunks("") == []
