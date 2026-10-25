"""The improve recipe: the ideator and the critic make a plan, the plan becomes an issue with checkboxes, and the
ideas the owner ticks become pull requests."""

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.pack import Pack
from lastcall.quota import Gate, Reading, Window
from lastcall.recipes.improve import ImproveReport, build_ideas, markdown, plan_body, publish, run_improve, ticked
from lastcall.runner import run_local

TIERS = {"fast": ("small", None), "balanced": ("mid", "high"), "deep": ("big", "high")}
READING = Reading("acct", (Window("week", 10.0, None, "expiring"),), False, "exact", 0.0)
PLAN = "https://github.com/ne0x86/demo/issues/7"


def idea(title: str, **changes: Any) -> dict[str, Any]:
    base = {
        "title": title,
        "kind": "usability",
        "why": "Users get stuck.",
        "evidence": "app.py:3",
        "change": "Say which file is missing.",
        "files": ["app.py"],
        "effort": "S",
        "risk": "low",
        "verification": "a test of the message",
    }
    return {**base, **changes}


def verdict(iid: str, kept: str = "keep", value: str = "medium", effort: str = "S") -> dict[str, Any]:
    return {
        "id": iid,
        "verdict": kept,
        "duplicate_of": "",
        "value": value,
        "effort": effort,
        "risk": "low",
        "reason": "r",
    }


def ok(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "tool", "count": 1}, {"kind": "result", "ok": True, "output": output}]


SCRIPT = {
    "ideator": ok(
        {
            "summary": "",
            "ideas": [
                idea("Name the missing file"),
                idea("Cache the report", kind="performance"),
                idea("Rewrite it"),
            ],
        }
    ),
    "critic": ok(
        {
            "summary": "Two ideas hold.",
            "verdicts": [
                verdict("I1", value="medium"),
                verdict("I2", value="high", effort="M"),
                verdict("I3", "too_large"),
                verdict("I9"),  # not an idea of this plan: dropped
            ],
        }
    ),
}


def pack(tmp_path: Path) -> Pack:
    return Pack("abc1234", tmp_path / "shared.md", tmp_path / "src", 1, [], 10)


def runner(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
    return run_local(engine, step)


def always(models: Sequence[str] = ()) -> tuple[Gate, Reading | None]:
    return Gate(True), READING


def plan(tmp_path: Path, script: dict[str, Any] = SCRIPT, check: Any = always, language: str = "English") -> Any:
    return run_improve(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(script),
        tiers=TIERS,
        run_step=runner,
        check_quota=check,
        language=language,
        earlier=[{"id": "#3", "state": "dismissed", "title": "Add a dark theme", "kind": "feature", "change": "x"}],
    )


class FakeIssues:
    def __init__(self) -> None:
        self.issues: list[tuple[str, str]] = []

    def open_issue(self, title: str, body: str) -> str:
        self.issues.append((title, body))
        return PLAN


def test_the_ideator_proposes_and_the_critic_keeps_what_holds(tmp_path: Path) -> None:
    report = plan(tmp_path)
    assert report.status == "ok"
    assert [s.role for s in report.steps] == ["ideator", "critic"]
    assert all(s.model == "big" for s in report.steps)
    assert [i["id"] for i in report.ideas] == ["I1", "I2", "I3"]
    assert [v["id"] for v in report.verdicts] == ["I1", "I2", "I3"]  # unknown ids are dropped
    assert report.plan == ["I2", "I1"]  # the most valuable first
    text = markdown(report)
    assert "## In the plan (2)" in text and "## Left out (1)" in text
    assert "**I3** Rewrite it: too_large. r" in text


def test_the_plan_is_an_issue_with_a_checkbox_per_idea(tmp_path: Path) -> None:
    report = publish(plan(tmp_path), issues := FakeIssues())
    assert report.issue_url == PLAN
    [(title, body)] = issues.issues
    assert title == "lastcall: 2 ideas for demo (improvement plan at abc1234)"
    assert "- [ ] **I2** Cache the report (performance · effort M · risk low) <!-- lastcall:idea I2 -->" in body
    assert "  *Change:* Say which file is missing." in body
    assert body.index("**I2**") < body.index("**I1**")
    assert "Tick the ones you want" in body
    assert publish(report, issues).issue_url == PLAN and len(issues.issues) == 1  # published once


def test_the_plan_speaks_the_runs_language(tmp_path: Path) -> None:
    body = plan_body(plan(tmp_path, language="Spanish"))
    assert "(rendimiento · esfuerzo M · riesgo bajo)" in body
    assert "*Cambio:*" in body and "Marca las que quieras" in body


def test_the_owners_ticks() -> None:
    body = plan_body(plan_report()).replace("- [ ] **I2**", "- [x] **I2**")
    assert ticked(body) == {"I2": True, "I1": False}
    assert ticked("* [X] **I5** Something <!-- lastcall:idea I5 -->\n- [ ] no mark here") == {"I5": True}


def plan_report() -> ImproveReport:
    r = ImproveReport("ne0x86/demo", "abc1234", "claude-code", "English")
    r.ideas = [{"id": "I1", **idea("Name the missing file")}, {"id": "I2", **idea("Cache the report")}]
    r.verdicts = [verdict("I1"), verdict("I2", value="high")]
    r.plan = ["I2", "I1"]
    return r


def test_a_plan_with_a_secret_is_not_published() -> None:
    report = plan_report()
    report.ideas[0]["evidence"] = "the key ghp_" + "a" * 36 + " in config.py"
    issues = FakeIssues()
    assert publish(report, issues).issue_url is None
    assert issues.issues == [] and "secret scan" in report.notes[0]


def test_nothing_worth_a_pull_request(tmp_path: Path) -> None:
    report = plan(tmp_path, {"ideator": ok({"summary": "", "ideas": []})})
    assert [s.role for s in report.steps] == ["ideator"]
    assert report.plan == [] and report.summary == "The ideator found nothing worth a pull request."


def test_a_closed_gate_stops_the_plan(tmp_path: Path) -> None:
    def closed(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        return Gate(False, "week big window at 95% (reserve 10%)"), READING

    report = plan(tmp_path, check=closed)
    assert report.status == "partial" and report.steps == [] and report.plan == []


# building the ideas the owner ticked


class FakeForge(FakeIssues):
    author = ("lastcall-demo[bot]", "1+lastcall-demo[bot]@users.noreply.github.com")

    def __init__(self) -> None:
        super().__init__()
        self.prs: list[tuple[str, str, str]] = []

    def push(self, workdir: Path, branch: str) -> None:
        pass

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        self.prs.append((branch, title, body))
        return f"https://github.com/ne0x86/demo/pull/{len(self.prs) + 10}"


def implementer(asked: list[str]) -> Callable[[Engine, StepSpec, Path], StepResult]:
    def run(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
        asked.append(step.prompt)
        (workspace / "app.py").write_text("def f(x):\n    return x[0] if x else None\n", encoding="utf-8")
        out = {
            "done": True,
            "summary": "Names the missing file.",
            "changes": [{"file": "app.py", "what": "message"}],
            "tests": {"command": "pytest", "result": "2 passed", "new": []},
            "pr_title": "Name the missing file",
            "pr_body": "**What and why.** Users got stuck.",
            "risks": "low",
            "pending": "",
        }
        return StepResult(True, out)

    return run


def test_ticked_ideas_become_pull_requests(tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]) -> None:
    (tmp_path / "clone").mkdir()
    make_repo(tmp_path / "clone", {"app.py": "def f(x):\n    return x[0]\n", "test_app.py": "def test_f(): pass\n"})
    (tmp_path / "shared.md").write_text("pack", encoding="utf-8")
    wanted = [{"id": "I2", "row": 12, "plan": PLAN, **idea("Name the missing file")}]
    asked: list[str] = []
    forge = FakeForge()
    report = build_ideas(
        report=ImproveReport("ne0x86/demo", "abc1234", "claude-code", "English"),
        wanted=wanted,
        run_dir=tmp_path,
        shared=tmp_path / "shared.md",
        engine=Scripted({}),
        tiers=TIERS,
        run_step=implementer(asked),
        check_quota=always,
        verify=lambda work, command: (True, "2 passed in 0.10s"),
        forge=forge,
    )
    [record] = report.deliveries
    assert record["status"] == "opened" and record["row"] == 12 and record["ids"] == ["I2"]
    assert "The owner chose this improvement from lastcall's plan (" + PLAN + ")" in asked[0]
    [(branch, title, body)] = forge.prs
    assert branch.startswith("lastcall/") and branch.endswith("-1-name-the-missing-file")
    assert title == "Name the missing file"
    assert f"lastcall: idea I2 of the improvement plan {PLAN}" in body
    log = subprocess.run(
        ["git", "-C", str(tmp_path / "pr1"), "log", "-1", "--format=%B"], capture_output=True, text=True
    )
    assert f"Idea: I2 of {PLAN}" in log.stdout
