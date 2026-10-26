"""The tests recipe (DESIGN.md, section 9): the audit's shape, find → judge → fix → verify → deliver, aimed at the
behavior that no test checks. One reviewer finds it, the judge keeps what matters and what tests can check as the
code is today, and the test writer adds the tests, one pull request each. lastcall refuses a change to anything
that is not a test, and checks the tests in the project's environment like any other change.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from lastcall.recipes.audit import Recipe, Reviewer

TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "testdata", "fixtures", "__snapshots__"}
TEST_DIR_ENDS = (".test", ".tests", "_test", "_tests", "-test", "-tests")  # MyApp.Tests, api_tests
TEST_NAME = re.compile(
    r"^test_.*\.py$|_test\.py$|^conftest\.py$"  # Python
    r"|_test\.go$"  # Go
    r"|\.(test|spec)\.[cm]?[jt]sx?$"  # JavaScript and TypeScript
    r"|Tests?\.(java|kt|cs)$"  # Java, Kotlin, C#
    r"|_(spec|test)\.rb$"  # Ruby
)


def is_test(path: str) -> bool:
    """Whether a path of the repository is a test: in a tests folder, or named the way tests are named in the usual
    languages. Tests inside a source file, such as Rust's `#[cfg(test)]`, are not."""
    *folders, name = PurePosixPath(path).parts
    return any(f.lower() in TEST_DIRS or f.lower().endswith(TEST_DIR_ENDS) for f in folders) or bool(
        TEST_NAME.search(name)
    )


TESTER = Reviewer("tests", "T", "balanced", "")  # its whole prompt is test_reviewer.md
TESTS = Recipe("tests", "test_reviewer.md", "test_judge.md", "test_writer.md", "pr.tests.footer", is_test)
