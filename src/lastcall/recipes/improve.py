"""The improve recipe (DESIGN.md, section 9): ideate, check, and a plan the owner chooses from.

The ideator reads the shared context pack and proposes improvements that fit the project: a small feature that
completes one it has, an error message that leaves users stuck, documentation that the code contradicts, a slow
path. The critic checks every idea against the code and keeps those worth one pull request each. They go to one
issue with a checkbox each, the improvement plan: the owner ticks the ones to build, and a later run builds them,
one pull request each, as an audit builds its fixes (lastcall.recipes.build).
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from lastcall import i18n
from lastcall.engines.base import Engine
from lastcall.environment import RUNNER, Environment
from lastcall.pack import Pack
from lastcall.recipes import prompt, render
from lastcall.recipes.build import Forge, Job, Verifier, build
from lastcall.recipes.steps import QuotaCheck, StepRecord, StepRunner, Steps, table
from lastcall.secretscan import scan

MAX_IDEAS = 6  # in one plan: each one costs the owner a read
VALUE = {"high": 0, "medium": 1, "low": 2}
EFFORT = {"S": 0, "M": 1, "L": 2}
# A plan's checkbox: `- [x] **I2** Title (...) <!-- lastcall:idea I2 -->`. The hidden mark survives the owner's ticks.
TICK = re.compile(r"^\s*[-*+] \[([ xX])\] .*<!-- lastcall:idea (I\d+) -->\s*$", re.MULTILINE)


class Issues(Protocol):
    def open_issue(self, title: str, body: str) -> str: ...


@dataclass
class ImproveReport:
    repo: str
    sha: str
    engine: str
    language: str
    status: str = "ok"  # ok | partial (stopped by a gate) | failed
    stopped: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    ideas: list[dict[str, Any]] = field(default_factory=list)  # the ideator's, with ids I1, I2...
    verdicts: list[dict[str, Any]] = field(default_factory=list)  # the critic's
    plan: list[str] = field(default_factory=list)  # the ids it keeps, most valuable first
    summary: str = ""
    issue_url: str | None = None  # the plan, once published
    deliveries: list[dict[str, Any]] = field(default_factory=list)  # ideas the owner chose, once built or tried
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> ImproveReport:
        data = json.loads(text)
        data["steps"] = [StepRecord(**s) for s in data.get("steps") or []]
        return cls(**data)


def run_improve(
    *,
    repo: str,
    pack: Pack,
    engine: Engine,
    tiers: Mapping[str, tuple[str, str | None]],
    run_step: StepRunner,
    check_quota: QuotaCheck,
    language: str = "English",
    max_ideas: int = MAX_IDEAS,
    earlier: Sequence[Mapping[str, Any]] = (),
    on_step: Callable[[StepRecord], None] | None = None,
) -> ImproveReport:
    """The ideator, then the critic: the plan is the ideas the critic keeps, most valuable and smallest first."""
    report = ImproveReport(repo, pack.sha, engine.name, language)
    steps = Steps(report, engine, tiers, run_step, check_quota, pack.path, on_step)
    project = repo.split("/")[-1]
    previous = json.dumps(list(earlier), ensure_ascii=False, indent=1) if earlier else "[]"
    text = render(
        prompt("ideator.md"), project=project, max_ideas=str(max_ideas * 2), previous=previous, language=language
    )
    res = steps.run("ideator", "deep", text, "ideas", pack.src)
    if res is None:
        return report
    if not res.ok:
        report.status = "failed"
        return report
    found = (res.output or {}).get("ideas") or []
    report.ideas = [{"id": f"I{n}", **idea} for n, idea in enumerate(found[: max_ideas * 2], 1)]
    if not report.ideas:
        report.summary = "The ideator found nothing worth a pull request."
        return report

    text = render(
        prompt("critic.md"),
        project=project,
        max_ideas=str(max_ideas),
        previous=previous,
        ideas=json.dumps(report.ideas, ensure_ascii=False, indent=1),
        language=language,
    )
    res = steps.run("critic", "deep", text, "idea_verdicts", pack.src)
    if res is None:
        return report
    if not res.ok:
        report.status = "failed"
        return report
    out = res.output or {}
    known = {idea["id"] for idea in report.ideas}
    report.summary = str(out.get("summary") or "")
    report.verdicts = [v for v in out.get("verdicts") or [] if v.get("id") in known]
    kept = [v for v in report.verdicts if v.get("verdict") == "keep"]
    kept.sort(key=lambda v: (VALUE.get(str(v.get("value")), 3), EFFORT.get(str(v.get("effort")), 3)))
    report.plan = list(dict.fromkeys(v["id"] for v in kept))[:max_ideas]
    return report


def publish(report: ImproveReport, forge: Issues) -> ImproveReport:
    """The plan as one issue, with a checkbox per idea, unless the secret scan finds something in it."""
    if not report.plan or report.issue_url:
        return report
    title, body = plan_title(report), plan_body(report)
    leaks = scan(f"{title}\n{body}")
    if leaks:
        report.notes.append(f"the plan was not published: the secret scan found {', '.join(leaks)}")
        return report
    report.issue_url = forge.open_issue(title, body)
    return report


def plan_title(report: ImproveReport) -> str:
    noun = i18n.text(report.language, "plan.one" if len(report.plan) == 1 else "plan.other")
    project = report.repo.split("/")[-1]
    return i18n.text(
        report.language, "plan.title", count=len(report.plan), noun=noun, project=project, sha=report.sha
    )


def plan_body(report: ImproveReport) -> str:
    def say(key: str, **values: str) -> str:
        return i18n.text(report.language, key, **values)

    ideas = {idea["id"]: idea for idea in report.ideas}
    verdicts = {v["id"]: v for v in report.verdicts}
    lines = [say("plan.intro"), ""]
    for iid in report.plan:
        idea, v = ideas[iid], verdicts[iid]
        meta = say(
            "plan.meta",
            kind=say(f"plan.kind.{idea['kind']}"),
            effort=str(v["effort"]),
            risk=say(f"plan.risk.{v['risk']}"),
        )
        lines += [
            f"- [ ] **{iid}** {idea['title']} ({meta}) <!-- lastcall:idea {iid} -->",
            f"  {idea['why']}",
            f"  *{say('plan.change')}:* {idea['change']}",
            f"  *{say('plan.evidence')}:* {idea['evidence']}",
            f"  *{say('plan.check')}:* {idea['verification']}",
        ]
    footer = say("plan.footer", sha=report.sha, engine=report.engine)
    return "\n".join([*lines, "", f"<sub>{footer}</sub>"])


def build_ideas(
    *,
    report: ImproveReport,
    wanted: Sequence[Mapping[str, Any]],
    run_dir: Path,
    shared: Path,
    engine: Engine,
    tiers: Mapping[str, tuple[str, str | None]],
    run_step: StepRunner,
    check_quota: QuotaCheck,
    verify: Verifier,
    forge: Forge,
    environment: Environment = RUNNER,
    max_prs: int = 3,
    on_step: Callable[[StepRecord], None] | None = None,
) -> ImproveReport:
    """The ideas the owner ticked, the oldest first, one pull request each. `wanted` holds each idea as the plan
    gave it, with `row` (its id in the store) and `plan` (the plan's URL)."""
    steps = Steps(report, engine, tiers, run_step, check_quota, shared, on_step)
    project, stamp = report.repo.split("/")[-1], time.strftime("%Y%m%d-%H%M")
    for n, idea in enumerate(wanted[:max_prs], 1):
        about = {k: idea.get(k) for k in ("title", "kind", "why", "evidence", "change", "files", "verification")}

        def ask(branch: str, check: str, idea: Mapping[str, Any] = idea, about: dict[str, Any] = about) -> str:
            return render(
                prompt("builder.md"),
                project=project,
                branch=branch,
                plan=str(idea["plan"]),
                title=str(idea["title"]),
                idea=json.dumps(about, ensure_ascii=False, indent=1),
                check=check,
                language=report.language,
            )

        def footer(checked: str, model: str, effort: str | None, idea: Mapping[str, Any] = idea) -> str:
            values = {"ids": str(idea["id"]), "plan": str(idea["plan"]), "model": model, "effort": effort or "-"}
            return i18n.text(report.language, "pr.idea.footer", checked=checked, **values)

        job = Job(str(idea["title"]), ask, f"Idea: {idea['id']} of {idea['plan']}", footer)
        record = build(
            job,
            {"title": idea["title"], "ids": [idea["id"]], "row": idea["row"]},
            report.deliveries,
            n=n,
            stamp=stamp,
            steps=steps,
            run_dir=run_dir,
            tiers=tiers,
            environment=environment,
            verify=verify,
            forge=forge,
            language=report.language,
        )
        if record.get("status") == "stopped":  # a gate closed: the rest waits for the next run
            break
    return report


def ticked(body: str) -> dict[str, bool]:
    """The plan's checkboxes, by idea: whether the owner ticked each one."""
    return {m.group(2): m.group(1) != " " for m in TICK.finditer(body)}


def markdown(report: ImproveReport) -> str:
    """The report for the owner: the plan, what was left out and why, what was built, and what each step cost."""
    ideas = {idea["id"]: idea for idea in report.ideas}
    verdicts = {v["id"]: v for v in report.verdicts}
    out = [f"# Improvement plan for {report.repo} @ {report.sha} ({report.engine})", ""]
    out.append(f"Status: **{report.status}**" + (f" ({report.stopped})" if report.stopped else ""))
    out += [f"- {note}" for note in report.notes]
    if report.summary:
        out += ["", report.summary]
    if report.deliveries:
        out += ["", "## Built", ""]
        for d in report.deliveries:
            out.append(f"- {d['title']} ({', '.join(d['ids'])}): **{d.get('status', 'pending')}** ")
            out[-1] += (d.get("url") or d.get("note") or "")[:300]
    if report.ideas:
        out += ["", f"## In the plan ({len(report.plan)})", ""]
        if report.issue_url:
            out += [f"The plan: {report.issue_url}", ""]
        for iid in report.plan:
            v = verdicts[iid]
            out.append(f"- **{iid}** {ideas[iid]['title']} ({v['value']} value, {v['effort']}, {v['risk']} risk)")
        left = [v for v in report.verdicts if v["id"] not in report.plan]
        out += ["", f"## Left out ({len(left)})", ""]
        out += [f"- **{v['id']}** {ideas[v['id']]['title']}: {v['verdict']}. {v['reason']}" for v in left]
    return "\n".join([*out, "", "## Steps", "", *table(report.steps), ""])
