"""The audit recipe, first half: find and judge (DESIGN.md, section 9).

Reviewers read the shared context pack, one after another so that the provider's cache serves all but the first,
and report findings with evidence. The judge checks every finding against the code and decides what becomes a pull
request, what needs the owner, and what is discarded. The second half (implement, verify, deliver) starts from
this report.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.pack import Pack
from lastcall.quota import Gate, Reading
from lastcall.recipes import front_matter, prompt, render, schema
from lastcall.tiers import TIERS

StepRunner = Callable[[Engine, StepSpec, Path], StepResult]  # (engine, step, workspace)
QuotaCheck = Callable[[], tuple[Gate, Reading | None]]


@dataclass(frozen=True)
class Reviewer:
    name: str
    prefix: str  # finding ids: B1, S2...
    tier: str
    focus: str


def reviewers() -> list[Reviewer]:
    """The built-in reviewers, from lastcall/prompts/reviewers/*.md."""
    found = []
    for item in sorted(resources.files("lastcall.prompts").joinpath("reviewers").iterdir(), key=lambda i: i.name):
        if not item.name.endswith(".md"):
            continue
        meta, body = front_matter(item.read_text(encoding="utf-8"))
        if meta.get("tier") not in TIERS or not meta.get("prefix") or not meta.get("name"):
            raise ValueError(f"reviewer {item.name} needs name, prefix and a tier among {TIERS}")
        found.append(Reviewer(meta["name"], meta["prefix"], meta["tier"], body))
    return found


@dataclass
class StepRecord:
    role: str
    model: str
    effort: str | None
    ok: bool
    seconds: float
    tool_uses: int
    cost_usd: float | None
    error: str | None
    quota_before: dict[str, float] | None  # window name -> % used, to measure what each step costs


@dataclass
class AuditReport:
    repo: str
    sha: str
    engine: str
    language: str
    status: str = "ok"  # ok | partial (stopped by a gate) | failed
    stopped: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    verdicts: list[dict[str, Any]] = field(default_factory=list)
    prs: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)


def run_audit(
    *,
    repo: str,
    pack: Pack,
    engine: Engine,
    tiers: Mapping[str, tuple[str, str | None]],
    run_step: StepRunner,
    check_quota: QuotaCheck,
    language: str = "English",
    max_prs: int = 3,
    max_per_pr: int = 3,
    previous: str = "None yet.",
    team: Sequence[Reviewer] | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
) -> AuditReport:
    report = AuditReport(repo, pack.sha, engine.name, language)
    project = repo.split("/")[-1]

    def step(role: str, tier: str, text: str, schema_name: str) -> StepResult | None:
        gate, reading = check_quota()
        if not gate.ok:
            report.status, report.stopped = "partial", gate.reason
            return None
        model, effort = tiers[tier]
        spec = StepSpec(role, model, text, effort, pack.path, schema(schema_name))
        t0 = time.monotonic()
        res = run_step(engine, spec, pack.src)
        usage = {w.name: w.used_pct for w in reading.windows} if reading else None
        record = StepRecord(
            role,
            model,
            effort,
            res.ok,
            round(time.monotonic() - t0, 1),
            res.tool_uses,
            res.cost_usd,
            res.error,
            usage,
        )
        report.steps.append(record)
        if on_step:
            on_step(record)
        return res

    for rv in team if team is not None else reviewers():
        text = render(prompt("reviewer.md"), role=rv.name, project=project, focus=rv.focus, language=language)
        res = step(f"reviewer:{rv.name}", rv.tier, text, "findings")
        if res is None:
            return report
        if res.ok:  # a reviewer that fails costs its findings, not the run
            for i, finding in enumerate((res.output or {}).get("findings") or [], 1):
                report.findings.append({"id": f"{rv.prefix}{i}", "reviewer": rv.name, **finding})
    if not report.findings:
        report.summary = "The reviewers found nothing worth a finding."
        report.status = "ok" if any(s.ok for s in report.steps) else "failed"
        return report

    text = render(
        prompt("judge.md"),
        project=project,
        max_prs=str(max_prs),
        max_per_pr=str(max_per_pr),
        language=language,
        previous=previous,
        findings=json.dumps(report.findings, ensure_ascii=False, indent=1),
    )
    res = step("judge", "deep", text, "verdicts")
    if res is None:
        return report
    if not res.ok:
        report.status = "failed"
        return report
    out = res.output or {}
    known = {f["id"] for f in report.findings}
    report.summary = str(out.get("summary") or "")
    report.verdicts = [v for v in out.get("verdicts") or [] if v.get("id") in known]
    for pr in (out.get("prs") or [])[:max_prs]:
        ids = [i for i in pr.get("ids") or [] if i in known][:max_per_pr]
        if ids:
            report.prs.append({**pr, "ids": ids})
    return report


def markdown(report: AuditReport) -> str:
    """The report for the owner: planned pull requests, decisions, what was discarded and what each step cost."""
    by_id = {f["id"]: f for f in report.findings}
    verdict = {v["id"]: v for v in report.verdicts}

    def line(fid: str) -> str:
        f, v = by_id[fid], verdict.get(fid, {})
        return f"- **{fid}** {f['title']} ({v.get('severity', f['severity'])}), `{f['file']}:{f['lines']}`: " + str(
            v.get("reason") or f["why"]
        )

    out = [f"# Audit of {report.repo} @ {report.sha} ({report.engine})", ""]
    out.append(f"Status: **{report.status}**" + (f" ({report.stopped})" if report.stopped else ""))
    if report.summary:
        out += ["", report.summary]
    out += ["", f"## Pull requests planned ({len(report.prs)})"]
    for n, pr in enumerate(report.prs, 1):
        out += ["", f"### {n}. {pr['title']} (risk {pr['risk']})", "", pr["plan"], ""]
        out += [line(i) for i in pr["ids"]]
    decide = [v["id"] for v in report.verdicts if v["verdict"] == "valid" and v["decision"] == "issue"]
    out += ["", f"## Needs your decision ({len(decide)})", ""] + [line(i) for i in decide]
    planned = {i for pr in report.prs for i in pr["ids"]}
    pending = [v["id"] for v in report.verdicts if v["decision"] == "pr" and v["id"] not in planned]
    if pending:
        out += ["", f"## Valid, for a later run ({len(pending)})", ""] + [line(i) for i in pending]
    dropped = [v for v in report.verdicts if v["decision"] == "discard"]
    out += ["", f"## Discarded ({len(dropped)})", ""]
    out += [f"- **{v['id']}** {by_id[v['id']]['title']}: {v['verdict']}. {v['reason']}" for v in dropped]
    out += [
        "",
        "## Steps",
        "",
        "| step | model | ok | seconds | tools | cost (USD eq.) | quota before |",
        "|---|---|---|---|---|---|---|",
    ]
    for s in report.steps:
        quota = ", ".join(f"{k} {v:g}%" for k, v in (s.quota_before or {}).items()) or "-"
        cost = f"{s.cost_usd:.2f}" if s.cost_usd is not None else "-"
        out.append(
            f"| {s.role} | {s.model} {s.effort or ''} | {'yes' if s.ok else 'no'} | {s.seconds:g} | "
            f"{s.tool_uses} | {cost} | {quota} |"
        )
    return "\n".join(out) + "\n"
