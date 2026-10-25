import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.quota import Gate, Reading
from lastcall.recipes.audit import AuditReport, deliver, slug

TIERS = {"fast": ("small", None), "balanced": ("mid", "high"), "deep": ("big", "high")}
FINDING = {
    "title": "Crash on empty input",
    "severity": "high",
    "confidence": "high",
    "file": "app.py",
    "lines": "1-2",
    "evidence": "line 1",
    "why": "it breaks",
    "fix": "guard it",
    "effort": "S",
    "fix_risk": "low",
    "verification": "a test",
}


def report() -> AuditReport:
    r = AuditReport("ne0x86/demo", "abc1234", "claude-code", "English")
    r.findings = [{"id": "B1", "reviewer": "bugs", **FINDING}, {"id": "S1", "reviewer": "security", **FINDING}]
    r.verdicts = [
        {"id": "B1", "verdict": "valid", "duplicate_of": "", "severity": "high", "decision": "pr", "reason": "r"},
        {"id": "S1", "verdict": "valid", "duplicate_of": "", "severity": "low", "decision": "issue", "reason": "q"},
    ]
    r.prs = [{"title": "Guard the empty input", "ids": ["B1"], "risk": "low", "plan": "Check it."}]
    return r


class FakeForge:
    author = ("lastcall-demo[bot]", "1+lastcall-demo[bot]@users.noreply.github.com")

    def __init__(self) -> None:
        self.pushed: list[str] = []
        self.prs: list[tuple[str, str, str]] = []
        self.issues: list[tuple[str, str]] = []

    def push(self, workdir: Path, branch: str) -> None:
        self.pushed.append(branch)

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        self.prs.append((branch, title, body))
        return f"https://github.com/ne0x86/demo/pull/{len(self.prs)}"

    def open_issue(self, title: str, body: str) -> str:
        self.issues.append((title, body))
        return "https://github.com/ne0x86/demo/issues/9"


def output(**changes: Any) -> dict[str, Any]:
    base = {
        "done": True,
        "summary": "Guards the empty input.",
        "changes": [{"file": "app.py", "what": "guard"}],
        "tests": {"command": "pytest", "result": "2 passed", "new": ["test_app.py::test_empty"]},
        "pr_title": "Guard the empty input",
        "pr_body": "**What and why.** It crashed.",
        "risks": "low",
        "pending": "",
    }
    return {**base, **changes}


def implementer(text: str | None, out: dict[str, Any]) -> Callable[[Engine, StepSpec, Path], StepResult]:
    """Stands in for the agent: edits app.py in the workspace (when text is given) and answers `out`."""

    def run(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
        assert step.writable and step.role == "implementer"
        if text is not None:
            (workspace / "app.py").write_text(text, encoding="utf-8")
        return StepResult(True, out)

    return run


def always() -> tuple[Gate, Reading | None]:
    return Gate(True), None


@pytest.fixture
def run_dir(tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]) -> Path:
    (tmp_path / "clone").mkdir()
    make_repo(tmp_path / "clone", {"app.py": "def f(x):\n    return x[0]\n", "test_app.py": "def test_f(): pass\n"})
    (tmp_path / "shared.md").write_text("pack", encoding="utf-8")
    return tmp_path


def run(run_dir: Path, rep: AuditReport, step: Any, forge: FakeForge, passed: bool = True) -> AuditReport:
    def verify(work: Path, command: str) -> tuple[bool, str]:
        assert "pytest" in command  # the repository has tests
        return passed, "collected 2 items\n2 passed in 0.10s" if passed else "1 failed"

    return deliver(
        report=rep,
        run_dir=run_dir,
        engine=Scripted({}),
        tiers=TIERS,
        run_step=step,
        check_quota=always,
        verify=verify,
        forge=forge,
    )


def test_a_verified_change_becomes_a_pull_request(run_dir: Path) -> None:
    forge = FakeForge()
    rep = run(run_dir, report(), implementer("def f(x):\n    return x[0] if x else None\n", output()), forge)
    (delivery,) = rep.deliveries
    assert delivery["status"] == "opened"
    assert delivery["url"] == "https://github.com/ne0x86/demo/pull/1"
    branch, title, body = forge.prs[0]
    assert forge.pushed == [branch]
    assert branch.startswith("lastcall/") and branch.endswith("-1-guard-the-empty-input")
    assert title == "Guard the empty input"
    assert "findings B1" in body and "`2 passed in 0.10s`" in body and "secret scan: clean" in body
    log = subprocess.run(
        ["git", "-C", str(run_dir / "pr1"), "log", "-1", "--format=%an <%ae>%n%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("lastcall-demo[bot] <1+lastcall-demo[bot]@users.noreply.github.com>")
    assert "Findings: B1" in log
    assert rep.issue_url == "https://github.com/ne0x86/demo/issues/9"
    assert forge.issues[0][0] == "lastcall: 1 decision for demo (audit of abc1234)"
    assert "**S1** Crash on empty input" in forge.issues[0][1]


def test_red_checks_open_nothing(run_dir: Path) -> None:
    forge = FakeForge()
    rep = run(run_dir, report(), implementer("def f(x):\n    return None\n", output()), forge, passed=False)
    assert rep.deliveries[0]["status"] == "red"
    assert forge.pushed == [] and forge.prs == []


def test_a_secret_in_the_change_blocks_it(run_dir: Path) -> None:
    forge = FakeForge()
    leak = "TOKEN = 'ghp_" + "a1B2c3D4" * 5 + "'\n"
    rep = run(run_dir, report(), implementer(leak, output()), forge)
    assert rep.deliveries[0]["status"] == "secrets"
    assert "GitHub token" in rep.deliveries[0]["note"]
    assert forge.pushed == []


def test_not_done_and_unchanged(run_dir: Path) -> None:
    forge = FakeForge()
    rep = run(run_dir, report(), implementer(None, output(done=False, pending="Not real.")), forge)
    assert (rep.deliveries[0]["status"], rep.deliveries[0]["note"]) == ("not_done", "Not real.")
    rep = run(run_dir, report(), implementer(None, output()), forge)
    assert rep.deliveries[0]["status"] == "unchanged"
    assert forge.prs == []


def test_delivering_twice_opens_nothing_new(run_dir: Path) -> None:
    forge = FakeForge()
    rep = run(run_dir, report(), implementer("def f(x):\n    return None if not x else x[0]\n", output()), forge)
    rep = run(run_dir, AuditReport.from_json(rep.to_json()), implementer("other\n", output()), forge)
    assert len(forge.prs) == 1
    assert len(forge.issues) == 1
    assert rep.deliveries[0]["status"] == "opened"


def test_a_closed_gate_stops_delivery(run_dir: Path) -> None:
    def closed() -> tuple[Gate, Reading | None]:
        return Gate(False, "week window at 91%"), None

    rep = deliver(
        report=report(),
        run_dir=run_dir,
        engine=Scripted({}),
        tiers=TIERS,
        run_step=implementer("x\n", output()),
        check_quota=closed,
        verify=lambda w, c: (True, ""),
        forge=FakeForge(),
    )
    assert rep.deliveries[0]["status"] == "stopped"
    assert rep.status == "partial"


def test_slug() -> None:
    assert slug("Códigos de salida fieles: diagnóstico y publicar") == "codigos-de-salida-fieles-diagnostico-y-p"
    assert slug("!!!") == "change"


def test_checks_run_in_the_projects_environment(run_dir: Path) -> None:
    from lastcall.environment import Environment

    prompts: list[str] = []
    commands: list[str] = []
    edit = implementer("def f(x):\n    return x[0] if x else None\n", output())

    def step(engine: Engine, spec: StepSpec, workspace: Path) -> StepResult:
        prompts.append(spec.prompt)
        return edit(engine, spec, workspace)

    def verify(work: Path, command: str) -> tuple[bool, str]:
        commands.append(command)
        return True, "ok  \texample.com/demo\t0.01s"

    forge = FakeForge()
    env = Environment("golang:1.23", "go mod download", "go test ./...", "go.mod")
    rep = deliver(
        report=report(),
        run_dir=run_dir,
        engine=Scripted({}),
        tiers=TIERS,
        run_step=step,
        check_quota=always,
        verify=verify,
        forge=forge,
        environment=env,
    )
    assert rep.deliveries[0]["status"] == "opened"
    assert commands == ["go test ./..."]  # the project's command, not lastcall's guess for Python
    assert "the image `golang:1.23` (go.mod), which this container does not have" in prompts[0]
    assert "tests run again in a clean golang:1.23 container" in forge.prs[0][2]
