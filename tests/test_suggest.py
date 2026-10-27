from typing import Any

import pytest

from lastcall.suggest import suggest

NOW = 1_800_000_000.0  # 2027-01-15
ACTIVE = "2027-01-10T10:00:00Z"
CODE = [("src/app.py", 40_000), ("src/db.py", 30_000)]
TESTS = [("tests/test_app.py", 4_000)]
CI = [(".github/workflows/ci.yml", 800)]


def repo(**changes: Any) -> dict[str, Any]:
    base = {
        "full_name": "ne0x86/app",
        "language": "Python",
        "archived": False,
        "fork": False,
        "pushed_at": ACTIVE,
        "open_issues_count": 2,
    }
    return {**base, **changes}


def test_an_active_tested_project_is_suggested() -> None:
    s = suggest(repo(), CODE + TESTS + CI, NOW)
    assert s.suggested
    assert s.score == 30 + 20 + 5 + 15 + 2
    assert s.code_tokens == (40_000 + 30_000 + 4_000 + 800) // 4
    assert "tests" in s.reasons


@pytest.mark.parametrize(("changes", "why"), [({"archived": True}, "archived"), ({"fork": True}, "fork")])
def test_left_out(changes: dict[str, Any], why: str) -> None:
    s = suggest(repo(**changes), CODE, NOW)
    assert not s.suggested
    assert s.reasons == (f"left out: {why}",)


def test_an_empty_repository_is_left_out() -> None:
    assert suggest(repo(), [], NOW).reasons == ("left out: empty",)


def test_without_tests_fixes_can_only_build() -> None:
    s = suggest(repo(), CODE, NOW)
    assert "no tests: fixes can only be checked to build" in s.reasons


def test_a_language_without_default_environment() -> None:
    s = suggest(repo(language="Go"), [("main.go", 9_000), ("main_test.go", 2_000)], NOW)
    assert "Go: add a .lastcall.yml with its environment, or a devcontainer" in s.reasons
    assert "tests" in s.reasons


def test_old_projects_score_lower() -> None:
    assert (
        suggest(repo(pushed_at="2025-01-01T00:00:00Z"), CODE + TESTS, NOW).score
        < suggest(repo(), CODE + TESTS, NOW).score
    )


def test_vendored_and_large_files_do_not_count() -> None:
    files = [
        ("node_modules/x/index.js", 50_000),
        ("dist/app.min.js", 90_000),
        ("data.csv", 1_000),
        ("big.py", 500_000),
    ]
    assert suggest(repo(), [*files, ("app.py", 8_000)], NOW).code_tokens == 2_000


def test_large_and_tiny_projects() -> None:
    assert "large: an audit covers part of the code" in suggest(repo(), [("a.py", 100_000)] * 20, NOW).reasons
    assert "tiny" in suggest(repo(), [("a.py", 4_000)], NOW).reasons


@pytest.mark.parametrize(
    "path",
    ["tests/test_x.py", "test/a.js", "pkg/__tests__/a.ts", "src/a.test.tsx", "src/a.spec.js", "x_test.go"],
)
def test_test_detection(path: str) -> None:
    assert "tests" in suggest(repo(), [*CODE, (path, 100)], NOW).reasons


@pytest.mark.parametrize("path", ["go.mod", ".lastcall.yml", ".devcontainer/go/devcontainer.json", "pom.xml"])
def test_a_project_with_its_own_environment_is_not_held_back(path: str) -> None:
    files = [("main.go", 9_000), ("main_test.go", 2_000), (path, 200)]
    s = suggest(repo(language="Go"), files, NOW)
    assert s.score == suggest(repo(), [("app.py", 9_000), ("test_app.py", 2_000)], NOW).score
    assert f"checked in its own environment ({path})" in s.reasons


def test_a_repository_with_its_own_reviewers() -> None:
    own = [(".lastcall/reviewers/accessibility.md", 300), (".lastcall/reviewers/api.md", 400)]
    s = suggest(repo(), CODE + TESTS + own, NOW)
    assert "2\u00a0reviewers of its own" in s.reasons
    assert s.score == suggest(repo(), CODE + TESTS + [], NOW).score  # it says so; it does not rank higher
