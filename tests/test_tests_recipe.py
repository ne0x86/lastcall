"""The tests recipe: the audit's shape, aimed at the behavior that no test checks. Its pull requests change only
tests."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.pack import Pack
from lastcall.quota import Gate, Reading
from lastcall.recipes.audit import AuditReport, deliver, markdown, run_audit
from lastcall.recipes.tests import TESTER, TESTS, is_test
from lastcall.store import Store

TIERS = {"fast": ("small", None), "balanced": ("mid", "high"), "deep": ("big", "high")}
UNTESTED = {
    "title": "The rounding of prices has no test",
    "severity": "high",
    "confidence": "high",
    "file": "app.py",
    "lines": "1-2",
    "evidence": "round_price is called by checkout; no test calls it",
    "why": "a wrong rounding would charge customers the wrong amount",
    "fix": "tests/test_prices.py: halves, negatives, zero",
    "effort": "S",
    "fix_risk": "low",
    "verification": "pytest tests/test_prices.py",
}


def always(models: Sequence[str] = ()) -> tuple[Gate, Reading | None]:
    return Gate(True), None


@pytest.mark.parametrize(
    ("path", "test"),
    [
        ("tests/test_prices.py", True),
        ("pkg/prices_test.go", True),
        ("web/src/prices.test.ts", True),
        ("web/__tests__/cart.jsx", True),
        ("src/test/java/acme/PricesTest.java", True),
        ("spec/prices_spec.rb", True),
        ("Acme.Tests/PricesTests.cs", True),
        ("conftest.py", True),
        ("tests/integration.rs", True),
        ("app.py", False),
        ("src/lib.rs", False),  # Rust's tests inside a source file are out of reach
        ("pyproject.toml", False),
        ("package.json", False),
        ("latest/prices.py", False),
    ],
)
def test_what_counts_as_a_test(path: str, test: bool) -> None:
    assert is_test(path) is test


def test_the_tests_reviewer_and_judge_have_their_own_prompts(tmp_path: Path) -> None:
    asked: list[tuple[str, str]] = []

    def step(engine: Engine, spec: StepSpec, workspace: Path) -> StepResult:
        asked.append((spec.role, spec.prompt))
        if spec.role == "reviewer:tests":
            return StepResult(True, {"summary": "One gap.", "findings": [UNTESTED]})
        verdict = {"id": "T1", "verdict": "valid", "duplicate_of": "", "severity": "high", "decision": "pr"}
        prs = [{"title": "Tests for the rounding", "ids": ["T1"], "risk": "low", "plan": "Halves and zero."}]
        return StepResult(True, {"summary": "s", "verdicts": [{**verdict, "reason": "no test"}], "prs": prs})

    report = run_audit(
        repo="acme/shop",
        pack=Pack("abc1234", tmp_path / "shared.md", tmp_path / "src", 1, [], 10),
        engine=Scripted({}),
        tiers=TIERS,
        run_step=step,
        check_quota=always,
        team=[TESTER],
        recipe=TESTS,
    )
    assert report.recipe == "tests" and report.prs[0]["ids"] == ["T1"]
    assert [role for role, _ in asked] == ["reviewer:tests", "judge"]
    assert "behavior that matters and that no test checks" in asked[0][1]
    assert "the tests that lastcall may write for **shop**" in asked[1][1]
    assert markdown(report).startswith("# Tests for acme/shop @ abc1234")


class FakeForge:
    author = ("lastcall-acme[bot]", "1+lastcall-acme[bot]@users.noreply.github.com")

    def __init__(self) -> None:
        self.prs: list[tuple[str, str, str]] = []

    def push(self, workdir: Path, branch: str) -> None:
        pass

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        self.prs.append((branch, title, body))
        return "https://github.com/acme/shop/pull/8"

    def open_issue(self, title: str, body: str) -> str:
        raise AssertionError("no decisions here")


def writer(files: dict[str, str], asked: list[str]) -> Callable[[Engine, StepSpec, Path], StepResult]:
    def run(engine: Engine, spec: StepSpec, workspace: Path) -> StepResult:
        asked.append(spec.prompt)
        for rel, text in files.items():
            (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
            (workspace / rel).write_text(text, encoding="utf-8")
        out = {
            "done": True,
            "summary": "Tests for the rounding.",
            "changes": [],
            "tests": {"command": "pytest", "result": "3 passed", "new": ["tests/test_prices.py"]},
            "pr_title": "Tests for the rounding of prices",
            "pr_body": "**What the tests check.** Halves round up.",
            "risks": "none",
            "pending": "",
        }
        return StepResult(True, out)

    return run


def deliver_tests(tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path], files: dict[str, str]) -> Any:
    (tmp_path / "clone").mkdir()
    make_repo(
        tmp_path / "clone", {"app.py": "def round_price(x):\n    return round(x, 2)\n", "tests/test_app.py": ""}
    )
    (tmp_path / "shared.md").write_text("pack", encoding="utf-8")
    report = AuditReport("acme/shop", "abc1234", "claude-code", "English", recipe="tests")
    report.findings = [{"id": "T1", "reviewer": "tests", **UNTESTED}]
    report.verdicts = [{"id": "T1", "verdict": "valid", "duplicate_of": "", "severity": "high", "decision": "pr"}]
    report.prs = [{"title": "Tests for the rounding", "ids": ["T1"], "risk": "low", "plan": "Halves and zero."}]
    asked: list[str] = []
    forge = FakeForge()
    deliver(
        report=report,
        run_dir=tmp_path,
        engine=Scripted({}),
        tiers=TIERS,
        run_step=writer(files, asked),
        check_quota=always,
        verify=lambda work, command: (True, "3 passed in 0.05s"),
        forge=forge,
        recipe=TESTS,
    )
    return report, asked, forge


def test_tests_become_a_pull_request(tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]) -> None:
    report, asked, forge = deliver_tests(tmp_path, make_repo, {"tests/test_prices.py": "def test_half(): pass\n"})
    assert report.deliveries[0]["status"] == "opened"
    assert "You are lastcall's **test writer** in **shop**" in asked[0]
    [(_, title, body)] = forge.prs
    assert title == "Tests for the rounding of prices"
    assert "lastcall: tests for `abc1234`" in body and "they change only tests" in body


def test_a_change_to_the_code_opens_nothing(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    files = {"tests/test_prices.py": "def test_half(): pass\n", "app.py": "def round_price(x):\n    return x\n"}
    report, _, forge = deliver_tests(tmp_path, make_repo, files)
    [record] = report.deliveries
    assert record["status"] == "outside" and record["note"] == "it changed what it may not: app.py"
    assert forge.prs == []


def test_each_recipe_remembers_its_own_findings(tmp_path: Path) -> None:
    store = Store(tmp_path / "lastcall.db")
    for run, recipe, fid in (("r-audit", "audit", "B1"), ("r-tests", "tests", "T1")):
        store.start_run(run, "acme/shop", "claude", "burst", recipe=recipe)
        report = AuditReport("acme/shop", "abc1234", "claude-code", "English", recipe=recipe)
        report.findings = [{"id": fid, "reviewer": "x", **UNTESTED, "title": f"finding {fid}"}]
        report.verdicts = [{"id": fid, "verdict": "valid", "duplicate_of": "", "severity": "high", "decision": "pr"}]
        report.verdicts[0]["reason"] = "r"
        store.record_findings(run, report)
    assert [f["title"] for f in store.earlier("acme/shop")] == ["finding B1"]
    assert [f["title"] for f in store.earlier("acme/shop", "tests")] == ["finding T1"]
