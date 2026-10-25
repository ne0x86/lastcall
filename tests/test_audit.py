from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.pack import Pack
from lastcall.quota import Gate, Reading, SpendPolicy, Window, gate
from lastcall.recipes.audit import markdown, run_audit
from lastcall.runner import run_local

TIERS = {"fast": ("small", None), "balanced": ("mid", "high"), "deep": ("big", "high")}
READING = Reading("acct", (Window("week", 10.0, None, "expiring"),), False, "exact", 0.0)


def finding(title: str) -> dict[str, Any]:
    return {
        "title": title,
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


def ok(output: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"kind": "tool", "count": 1}, {"kind": "result", "ok": True, "output": output}]


def verdict(fid: str, decision: str) -> dict[str, Any]:
    return {
        "id": fid,
        "verdict": "valid",
        "duplicate_of": "",
        "severity": "high",
        "decision": decision,
        "reason": "r",
    }


SCRIPT = {
    "reviewer:bugs": ok({"summary": "", "findings": [finding("Crash on empty input")]}),
    "reviewer:hygiene": ok({"summary": "", "findings": []}),
    "reviewer:performance": [{"kind": "result", "ok": False, "error": "the model gave up"}],
    "reviewer:security": ok({"summary": "", "findings": [finding("Token written to the logs")]}),
    "judge": ok(
        {
            "summary": "Two valid findings.",
            "verdicts": [verdict("B1", "pr"), verdict("S1", "issue"), verdict("X9", "pr")],
            "prs": [
                {"title": "Guard empty input", "ids": ["B1", "X9"], "risk": "low", "plan": "Check the input."},
                {"title": "One too many", "ids": ["S1"], "risk": "low", "plan": "..."},
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


def test_reviewers_then_judge(tmp_path: Path) -> None:
    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=always,
        max_prs=1,
    )
    assert report.status == "ok"
    assert [f["id"] for f in report.findings] == ["B1", "S1"]
    assert [s.role for s in report.steps] == [
        "reviewer:bugs",
        "reviewer:hygiene",
        "reviewer:performance",
        "reviewer:security",
        "judge",
    ]
    assert not report.steps[2].ok  # a failed reviewer costs its findings, not the run
    assert report.steps[-1].model == "big"
    assert report.steps[0].quota_before == {"week": 10.0}
    assert [v["id"] for v in report.verdicts] == ["B1", "S1"]  # unknown ids are dropped
    assert report.prs == [{"title": "Guard empty input", "ids": ["B1"], "risk": "low", "plan": "Check the input."}]
    text = markdown(report)
    assert "## Pull requests planned (1)" in text
    assert "## Needs your decision (1)" in text
    assert "Token written to the logs" in text


def test_a_closed_gate_stops_the_run(tmp_path: Path) -> None:
    calls: list[int] = []

    def two_steps(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        calls.append(1)
        return (Gate(True), READING) if len(calls) <= 2 else (Gate(False, "5h window at 95%", 600.0), READING)

    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=two_steps,
    )
    assert report.status == "partial"
    assert report.stopped == "5h window at 95%"
    assert len(report.steps) == 2
    assert report.verdicts == []


def test_nothing_found(tmp_path: Path) -> None:
    quiet = {role: ok({"summary": "", "findings": []}) for role in SCRIPT if role != "judge"}
    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(quiet),
        tiers=TIERS,
        run_step=runner,
        check_quota=always,
    )
    assert report.status == "ok"
    assert report.summary == "The reviewers found nothing worth a finding."
    assert len(report.steps) == 4


def test_a_pending_finding_from_an_earlier_run_joins_a_pull_request(tmp_path: Path) -> None:
    earlier = [{"id": "#7", "state": "pending", **finding("Retry without backoff")}]
    script = dict(SCRIPT)
    script["judge"] = ok(
        {
            "summary": "",
            "verdicts": [verdict("B1", "pr")],
            "prs": [{"title": "Guard and retry", "ids": ["B1", "#7", "#9"], "risk": "low", "plan": "p"}],
        }
    )
    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(script),
        tiers=TIERS,
        run_step=runner,
        check_quota=always,
        earlier=earlier,
    )
    assert report.prs[0]["ids"] == ["B1", "#7"]  # unknown #9 dropped
    joined = next(f for f in report.findings if f["id"] == "#7")
    assert joined["reviewer"] == "earlier run"
    assert joined["title"] == "Retry without backoff"


def test_peers_review_the_same_pack_and_one_judge_weighs_everything(tmp_path: Path) -> None:
    from lastcall.recipes.audit import Peer

    lead = dict(SCRIPT)
    lead["judge"] = ok(
        {
            "summary": "One problem, found twice.",
            "verdicts": [
                verdict("B1", "pr"),
                verdict("S1", "issue"),
                {**verdict("B1@chatgpt", "discard"), "verdict": "duplicate", "duplicate_of": "B1"},
            ],
            "prs": [{"title": "Guard empty input", "ids": ["B1"], "risk": "low", "plan": "Check the input."}],
        }
    )
    theirs = {
        "reviewer:bugs@chatgpt": ok({"summary": "", "findings": [finding("Empty input crashes f")]}),
        "reviewer:hygiene@chatgpt": ok({"summary": "", "findings": []}),
        "reviewer:performance@chatgpt": ok({"summary": "", "findings": []}),
        "reviewer:security@chatgpt": ok({"summary": "", "findings": []}),
    }
    judged: list[str] = []

    def run_step(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
        if step.role == "judge":
            judged.append(step.prompt)
        return run_local(engine, step)

    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(lead),
        tiers=TIERS,
        run_step=run_step,
        check_quota=always,
        peers=[Peer("chatgpt", Scripted(theirs), TIERS, runner, always)],
    )
    assert [f["id"] for f in report.findings] == ["B1", "S1", "B1@chatgpt"]  # this account's first
    assert sorted(s.role for s in report.steps if s.role.endswith("@chatgpt")) == [
        "reviewer:bugs@chatgpt",
        "reviewer:hygiene@chatgpt",
        "reviewer:performance@chatgpt",
        "reviewer:security@chatgpt",
    ]
    assert len(judged) == 1 and '"B1@chatgpt"' in judged[0]  # one judge, for every finding
    assert report.peers == ["chatgpt"] and report.prs[0]["ids"] == ["B1"]
    assert "Also reviewed by: chatgpt" in markdown(report)


def test_a_peer_out_of_quota_stops_alone(tmp_path: Path) -> None:
    from lastcall.recipes.audit import Peer

    def closed(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        return Gate(False, "week window at 92% (reserve 10%)"), None

    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=always,
        peers=[Peer("chatgpt", Scripted({}), TIERS, runner, closed)],
    )
    assert report.status == "ok" and report.stopped is None  # the run goes on
    assert report.notes == ["chatgpt stopped reviewing: week window at 92% (reserve 10%)"]
    assert not any(s.role.endswith("@chatgpt") for s in report.steps)


def test_the_first_step_checks_every_model_of_the_run(tmp_path: Path) -> None:
    asked: list[list[str]] = []

    def check(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        asked.append(list(models))
        return Gate(True), READING

    run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=check,
    )
    # the first reviewer checks the reviewers' model and the judge's; then each step checks its own
    assert asked == [["mid", "big"], ["mid"], ["mid"], ["mid"], ["big"]]


def test_a_model_out_of_room_stops_the_run_before_it_spends(tmp_path: Path) -> None:
    """The judge's model has its own window, at the reserve: no reviewer runs, since the judge could not."""
    full = Reading(
        "acct",
        (Window("week", 10.0, None, "expiring"), Window("week", 95.0, None, "expiring", model="big")),
        False,
        "exact",
        0.0,
    )

    def check(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        return gate(full, SpendPolicy(), 0.0, models), full

    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=check,
    )
    assert report.status == "partial"
    assert report.stopped == "week big window at 95% (reserve 10%)"
    assert report.steps == []


def test_a_peer_checks_only_its_reviewers_models(tmp_path: Path) -> None:
    from lastcall.recipes.audit import Peer

    asked: list[list[str]] = []

    def peer_check(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        asked.append(list(models))
        return Gate(True), READING

    quiet = {
        f"reviewer:{r}@chatgpt": ok({"summary": "", "findings": []})
        for r in ("bugs", "hygiene", "performance", "security")
    }
    run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(SCRIPT),
        tiers=TIERS,
        run_step=runner,
        check_quota=always,
        peers=[Peer("chatgpt", Scripted(quiet), TIERS, runner, peer_check)],
    )
    assert asked[0] == ["mid"]  # a peer does not judge


def test_a_projects_reviewer_runs_with_the_others(tmp_path: Path) -> None:
    from lastcall.recipes.audit import Reviewer, reviewers

    asked: list[list[str]] = []

    def check(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        asked.append(list(models))
        return Gate(True), READING

    script = {**SCRIPT, "reviewer:accessibility": ok({"summary": "", "findings": [finding("Unlabelled button")]})}
    report = run_audit(
        repo="ne0x86/demo",
        pack=pack(tmp_path),
        engine=Scripted(script),
        tiers=TIERS,
        run_step=runner,
        check_quota=check,
        team=[*reviewers(), Reviewer("accessibility", "A", "fast", "Find WCAG 2.2 AA failures.")],
    )
    assert "reviewer:accessibility" in [s.role for s in report.steps]
    assert "A1" in [f["id"] for f in report.findings]
    assert asked[0] == ["mid", "small", "big"]  # its model is checked with the others before the first step
