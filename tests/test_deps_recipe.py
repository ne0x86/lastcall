"""The deps recipe: the dependencies behind their latest versions, from osv-scanner and deps.dev, and pull requests
that upgrade them and change the code that the new version breaks."""

import io
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from lastcall.engines.base import Engine, Invocation, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.pack import Pack
from lastcall.probes import deps as probe
from lastcall.process import Outcome
from lastcall.quota import Gate, Reading
from lastcall.recipes.audit import AuditReport, deliver, markdown, run_audit
from lastcall.recipes.deps import DEPS, UPGRADES
from lastcall.scanners import TITLES, VERSIONS, behind, dependencies

TIERS = {"fast": ("small", None), "balanced": ("mid", "high"), "deep": ("big", "high")}
OLD_FLASK = {
    "title": "Flask 2.0.0 to 3.1.3",
    "severity": "medium",
    "confidence": "high",
    "file": "requirements.txt",
    "lines": "2-2",
    "evidence": "requirements.txt:2 pins flask==2.0.0; app.py uses flask.json.JSONEncoder, removed in 2.3",
    "why": "2.0 no longer gets fixes",
    "fix": "flask==3.1.3; replace the JSON encoder with a provider",
    "effort": "M",
    "fix_risk": "medium",
    "verification": "pytest",
}


def always(models: Sequence[str] = ()) -> tuple[Gate, Reading | None]:
    return Gate(True), None


# what the pack says


def package(name: str, version: str, latest: str, deprecated: bool = False) -> dict[str, Any]:
    return {"system": "pypi", "name": name, "version": version, "latest": latest, "deprecated": deprecated,
            "reason": "use httpx" if deprecated else "", "source": "requirements.txt"}  # fmt: skip


def test_the_pack_lists_the_dependencies_behind_the_deprecated_first() -> None:
    text = behind(
        [
            package("requests", "2.19.0", "2.34.2"),
            package("flask", "2.0.0", "3.1.3"),
            package("certifi", "2026.9.1", "2026.9.1"),
            package("oldlib", "1.0", "1.0", deprecated=True),
        ]
    )
    assert text.splitlines() == [
        "3 of 4 direct dependencies have a newer version:",
        "- oldlib 1.0 → 1.0 (pypi, requirements.txt); DEPRECATED: use httpx",
        "- flask 2.0.0 → 3.1.3 (major) (pypi, requirements.txt)",
        "- requests 2.19.0 → 2.34.2 (pypi, requirements.txt)",
    ]
    assert (
        behind([package("certifi", "2026.9.1", "2026.9.1")])
        == "All 1 direct dependencies are at their latest version."
    )
    assert behind([]) == "No direct dependencies found in the dependency files."


class Box:
    """A sandbox whose step prints what the deps probe would."""

    def __init__(self, lines: list[str]) -> None:
        self.lines, self.invocations = lines, list[Invocation]()

    def call(self, engine: str, inv: Invocation, on_line: Callable[[str], None], **kw: Any) -> Outcome:
        self.invocations.append(inv)
        for line in self.lines:
            on_line(line)
        return Outcome(0, False, "")


def test_the_deps_sections_come_from_the_probe(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.12\nRUN pip install urllib3==1.24.1\n", encoding="utf-8")
    osv = {"results": [{"packages": [{"package": {"name": "urllib3", "version": "1.24.1"}, "vulnerabilities": [
        {"id": "GHSA-x", "summary": "CRLF injection", "affected": [{"ranges": [{"events": [{"fixed": "1.24.2"}]}]}]}
    ]}]}]}  # fmt: skip
    lines = [
        json.dumps({"tool": "osv-scanner", "ok": True, "data": osv}),
        json.dumps({"tool": "versions", "ok": True, "data": [package("urllib3", "1.24.1", "2.5.0")]}),
    ]
    box = Box(lines)
    found = dependencies(box, "claude-code", tmp_path)  # type: ignore[arg-type]
    assert (
        "urllib3 1.24.1: GHSA-x" in found[TITLES["osv-scanner"]]
        and "fixed in: 1.24.2" in found[TITLES["osv-scanner"]]
    )
    assert "- urllib3 1.24.1 → 2.5.0 (major)" in found[VERSIONS]
    (inv,) = box.invocations
    assert inv.argv == ("python3", "/ctx/deps.py", "/work", "/ctx/deps")
    assert inv.files["/ctx/deps/dockerfile-requirements.txt"] == "urllib3==1.24.1\n"  # pinned only in a Dockerfile
    assert "deps.dev" in inv.files["/ctx/deps.py"]
    failed = dependencies(Box([json.dumps({"tool": "versions", "ok": False, "error": "no network"})]), "x", tmp_path)  # type: ignore[arg-type]
    assert failed[VERSIONS] == "The dependencies were not read: no network"


# the probe, which runs in the runner


def test_the_probe_tells_direct_dependencies_by_their_manifests(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests==2.19.0\npython_dateutil>=2\n", encoding="utf-8")
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text('{"dependencies": {"lodash": "^4"}}', encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "package.json").write_text('{"name": "left-pad"}', encoding="utf-8")
    text = probe.manifests(tmp_path)
    assert probe.named(text, "requests") and not probe.named(text, "requests-oauthlib")
    assert probe.named(text, "python-dateutil")  # dashes and underscores stand for each other
    assert probe.named(text, "lodash") and not probe.named(text, "left-pad")  # node_modules is not the project's
    assert probe.named("<artifactId>commons-lang3</artifactId>", "org.apache.commons:commons-lang3")
    assert not probe.named("zope.interface", "zope")


def test_the_probe_reads_the_latest_version_from_deps_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []
    answer = {"versions": [
        {"versionKey": {"version": "2.19.0"}, "isDefault": False, "isDeprecated": False, "deprecatedReason": ""},
        {"versionKey": {"version": "2.34.2"}, "isDefault": True, "isDeprecated": False, "deprecatedReason": ""},
    ]}  # fmt: skip

    def urlopen(url: str, timeout: float) -> io.BytesIO:
        asked.append(url)
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    found = probe.latest({"system": "npm", "name": "@types/node", "version": "2.19.0", "source": "package.json"})
    assert asked == ["https://api.deps.dev/v3/systems/npm/packages/%40types%2Fnode"]
    assert (found["latest"], found["deprecated"]) == ("2.34.2", False)


# the recipe


def test_the_upgrades_have_their_own_reviewer_and_judge(tmp_path: Path) -> None:
    asked: list[tuple[str, str]] = []

    def step(engine: Engine, spec: StepSpec, workspace: Path) -> StepResult:
        asked.append((spec.role, spec.prompt))
        if spec.role == "reviewer:deps":
            return StepResult(True, {"summary": "One.", "findings": [OLD_FLASK]})
        verdict = {"id": "D1", "verdict": "valid", "duplicate_of": "", "severity": "medium", "decision": "pr"}
        prs = [{"title": "Upgrade Flask to 3.1", "ids": ["D1"], "risk": "medium", "plan": "Bump and adapt."}]
        return StepResult(True, {"summary": "s", "verdicts": [{**verdict, "reason": "used"}], "prs": prs})

    report = run_audit(
        repo="acme/shop",
        pack=Pack("abc1234", tmp_path / "shared.md", tmp_path / "src", 1, [], 10),
        engine=Scripted({}),
        tiers=TIERS,
        run_step=step,
        check_quota=always,
        team=[UPGRADES],
        recipe=DEPS,
    )
    assert report.recipe == "deps" and [role for role, _ in asked] == ["reviewer:deps", "judge"]
    assert "you choose the dependency upgrades worth a pull request now" in asked[0][1]
    assert "the dependency upgrades that lastcall may make for **shop**" in asked[1][1]
    assert markdown(report).startswith("# Dependency upgrades for acme/shop @ abc1234")


class FakeForge:
    author = ("lastcall-acme[bot]", "1+lastcall-acme[bot]@users.noreply.github.com")

    def __init__(self) -> None:
        self.prs: list[tuple[str, str, str]] = []

    def push(self, workdir: Path, branch: str) -> None:
        pass

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        self.prs.append((branch, title, body))
        return "https://github.com/acme/shop/pull/9"

    def open_issue(self, title: str, body: str) -> str:
        raise AssertionError("no decisions here")


def test_an_upgrade_changes_the_manifest_and_the_code(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    (tmp_path / "clone").mkdir()
    make_repo(tmp_path / "clone", {"requirements.txt": "flask==2.0.0\n", "app.py": "from flask.json import X\n"})
    (tmp_path / "shared.md").write_text("pack", encoding="utf-8")
    asked: list[str] = []

    def upgrader(engine: Engine, spec: StepSpec, workspace: Path) -> StepResult:
        asked.append(spec.prompt)
        (workspace / "requirements.txt").write_text("flask==3.1.3\n", encoding="utf-8")
        (workspace / "app.py").write_text("from flask.json.provider import DefaultJSONProvider\n", encoding="utf-8")
        out = {"done": True, "summary": "Flask 3.1.", "changes": [], "risks": "medium", "pending": "",
               "tests": {"command": "pytest", "result": "4 passed", "new": []},
               "pr_title": "Upgrade Flask from 2.0.0 to 3.1.3", "pr_body": "**Why.** 2.0 gets no fixes."}  # fmt: skip
        return StepResult(True, out)

    report = AuditReport("acme/shop", "abc1234", "claude-code", "English", recipe="deps")
    report.findings = [{"id": "D1", "reviewer": "deps", **OLD_FLASK}]
    report.verdicts = [{"id": "D1", "verdict": "valid", "duplicate_of": "", "severity": "medium", "decision": "pr"}]
    report.prs = [{"title": "Upgrade Flask to 3.1", "ids": ["D1"], "risk": "medium", "plan": "Bump and adapt."}]
    forge = FakeForge()
    deliver(
        report=report,
        run_dir=tmp_path,
        engine=Scripted({}),
        tiers=TIERS,
        run_step=upgrader,
        check_quota=always,
        verify=lambda work, command: (True, "4 passed in 0.20s"),
        forge=forge,
        recipe=DEPS,
    )
    assert report.deliveries[0]["status"] == "opened"  # the code may change with the manifest
    assert "You are lastcall's **upgrader** in **shop**" in asked[0]
    [(_, title, body)] = forge.prs
    assert title == "Upgrade Flask from 2.0.0 to 3.1.3" and "lastcall: dependency upgrades for `abc1234`" in body
