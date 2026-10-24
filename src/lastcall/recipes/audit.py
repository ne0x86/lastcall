"""The audit recipe (DESIGN.md, section 9), in two halves.

Find and judge (`run_audit`): reviewers read the shared context pack, one after another so that the provider's
cache serves all but the first, and report findings with evidence. The judge checks every finding against the code
and decides what becomes a pull request, what needs the owner, and what is discarded.

Fix, verify and deliver (`deliver`): one implementer per planned pull request, each in its own clone. lastcall, not
the agent, checks the change in a clean container, scans what it would publish for secrets, commits, pushes and
opens the pull request. Findings that need the owner go to one issue per run.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from lastcall import git, i18n
from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.pack import Pack
from lastcall.quota import Gate, Reading
from lastcall.recipes import front_matter, prompt, render, schema
from lastcall.secretscan import added_lines, scan
from lastcall.tiers import TIERS
from lastcall.verify import check_command, describe, summary_line

StepRunner = Callable[[Engine, StepSpec, Path], StepResult]  # (engine, step, workspace)
QuotaCheck = Callable[[], tuple[Gate, Reading | None]]
Verifier = Callable[[Path, str], tuple[bool, str]]  # (the change, check command) -> (passed, last lines of output)


class Forge(Protocol):
    @property
    def author(self) -> tuple[str, str]: ...  # name and email of the commits

    def push(self, workdir: Path, branch: str) -> None: ...

    def open_pull_request(self, branch: str, title: str, body: str) -> str: ...

    def open_issue(self, title: str, body: str) -> str: ...


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
    deliveries: list[dict[str, Any]] = field(default_factory=list)  # one per planned pull request, once tried
    issue_url: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> AuditReport:
        data = json.loads(text)
        data["steps"] = [StepRecord(**s) for s in data.get("steps") or []]
        return cls(**data)


@dataclass
class _Steps:
    """Runs the agent steps of a report: the gate before each one, and a record of what it cost."""

    report: AuditReport
    engine: Engine
    tiers: Mapping[str, tuple[str, str | None]]
    run_step: StepRunner
    check_quota: QuotaCheck
    shared: Path
    on_step: Callable[[StepRecord], None] | None = None

    def run(
        self, role: str, tier: str, text: str, schema_name: str, workspace: Path, writable: bool = False
    ) -> StepResult | None:
        gate, reading = self.check_quota()
        if not gate.ok:
            self.report.status, self.report.stopped = "partial", gate.reason
            return None
        model, effort = self.tiers[tier]
        spec = StepSpec(role, model, text, effort, self.shared, schema(schema_name), writable)
        t0 = time.monotonic()
        res = self.run_step(self.engine, spec, workspace)
        usage = {w.name: w.used_pct for w in reading.windows} if reading else None
        seconds = round(time.monotonic() - t0, 1)
        record = StepRecord(role, model, effort, res.ok, seconds, res.tool_uses, res.cost_usd, res.error, usage)
        self.report.steps.append(record)
        if self.on_step:
            self.on_step(record)
        return res


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
    earlier: Sequence[Mapping[str, Any]] = (),
    team: Sequence[Reviewer] | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
) -> AuditReport:
    """First half: find and judge."""
    report = AuditReport(repo, pack.sha, engine.name, language)
    steps = _Steps(report, engine, tiers, run_step, check_quota, pack.path, on_step)
    project = repo.split("/")[-1]

    for rv in team if team is not None else reviewers():
        text = render(prompt("reviewer.md"), role=rv.name, project=project, focus=rv.focus, language=language)
        res = steps.run(f"reviewer:{rv.name}", rv.tier, text, "findings", pack.src)
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
        previous=json.dumps(list(earlier), ensure_ascii=False, indent=1) if earlier else "[]",
        findings=json.dumps(report.findings, ensure_ascii=False, indent=1),
    )
    res = steps.run("judge", "deep", text, "verdicts", pack.src)
    if res is None:
        return report
    if not res.ok:
        report.status = "failed"
        return report
    out = res.output or {}
    known = {f["id"] for f in report.findings}
    pending = {str(e["id"]): e for e in earlier if e.get("state") == "pending"}
    report.summary = str(out.get("summary") or "")
    report.verdicts = [v for v in out.get("verdicts") or [] if v.get("id") in known]
    for pr in (out.get("prs") or [])[:max_prs]:
        ids = [i for i in pr.get("ids") or [] if i in known or i in pending][:max_per_pr]
        for i in ids:  # a pending finding from an earlier run joins this report, for its pull request
            if i in pending and all(f["id"] != i for f in report.findings):
                report.findings.append({**pending[i], "reviewer": "earlier run"})
        if ids:
            report.prs.append({**pr, "ids": ids})
    return report


def slug(title: str, size: int = 40) -> str:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:size].rstrip("-") or "change"


def deliver(
    *,
    report: AuditReport,
    run_dir: Path,
    engine: Engine,
    tiers: Mapping[str, tuple[str, str | None]],
    run_step: StepRunner,
    check_quota: QuotaCheck,
    verify: Verifier,
    forge: Forge,
    max_prs: int | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
) -> AuditReport:
    """Second half: implement each planned pull request, verify it outside the agent, and open it. Pull requests
    already opened for this report are not opened again."""
    steps = _Steps(report, engine, tiers, run_step, check_quota, run_dir / "shared.md", on_step)
    by_id = {f["id"]: f for f in report.findings}
    verdict = {v["id"]: v for v in report.verdicts}
    opened = {d["title"] for d in report.deliveries if d["status"] == "opened"}
    project, stamp = report.repo.split("/")[-1], time.strftime("%Y%m%d-%H%M")

    for n, pr in enumerate(report.prs[:max_prs] if max_prs else report.prs, 1):
        if pr["title"] in opened:
            continue
        branch = f"lastcall/{stamp}-{n}-{slug(pr['title'])}"
        work = run_dir / f"pr{n}"
        git.remove(work)
        git.branch_from(run_dir / "clone", work, branch)
        command, _ = check_command(work)
        with_tests, _ = check_command(work, assume_tests=True)  # what verification runs once tests exist
        covered = [dict(by_id[i], judge=verdict.get(i)) for i in pr["ids"]]
        text = render(
            prompt("implementer.md"),
            project=project,
            branch=branch,
            title=pr["title"],
            risk=pr["risk"],
            plan=pr["plan"],
            findings=json.dumps(covered, ensure_ascii=False, indent=1),
            test_command=command,
            verify_command=with_tests,
            language=report.language,
        )
        record: dict[str, Any] = {"title": pr["title"], "ids": pr["ids"], "branch": branch}
        report.deliveries = [d for d in report.deliveries if d["title"] != pr["title"]] + [record]
        res = steps.run("implementer", "deep", text, "implementation", work, writable=True)
        if res is None:  # a gate closed: the rest waits for the next run
            record |= {"status": "stopped", "note": report.stopped}
            break
        out = res.output or {}
        if not res.ok or not out.get("done"):
            record |= {"status": "not_done", "note": str(out.get("pending") or res.error or "")[:800]}
            continue
        diff = git.stage_all(work)
        if not diff.strip():
            record |= {"status": "unchanged", "note": "the implementer reported done but changed nothing"}
            continue
        command, kind = check_command(work)  # the implementer may have added the project's first tests
        passed, tail = verify(work, command)
        last = summary_line(tail)
        if not passed:
            record |= {"status": "red", "note": tail[-1500:]}
            continue
        title, body = str(out["pr_title"]), str(out["pr_body"])
        leaks = scan(f"{added_lines(diff)}\n{title}\n{body}")
        if leaks:
            record |= {"status": "secrets", "note": f"blocked by the secret scan: {', '.join(leaks)}"}
            continue
        model, effort = tiers["deep"]
        message = f"{title}\n\n{out.get('summary') or ''}\n\nFindings: {', '.join(pr['ids'])}"
        git.commit(work, message, forge.author)
        forge.push(work, branch)
        checked = describe(kind, last, report.language)
        footer = "\n\n---\n<sub>" + pr_footer(report, pr["ids"], model, effort, checked) + "</sub>"
        record |= {"status": "opened", "url": forge.open_pull_request(branch, title, body + footer), "check": checked}

    decide = [v for v in report.verdicts if v["verdict"] == "valid" and v["decision"] == "issue"]
    if decide and not report.issue_url:
        body = issue_body(report, decide, by_id)
        if not scan(body):
            report.issue_url = forge.open_issue(issue_title(report, len(decide)), body)
    return report


def pr_footer(report: AuditReport, ids: Sequence[str], model: str, effort: str | None, checked: str) -> str:
    return i18n.text(
        report.language,
        "pr.footer",
        sha=report.sha,
        engine=report.engine,
        ids=", ".join(ids),
        model=model,
        effort=effort or "-",
        checked=checked,
    )


def issue_title(report: AuditReport, count: int) -> str:
    noun = i18n.text(report.language, "issue.one" if count == 1 else "issue.other")
    project = report.repo.split("/")[-1]
    return i18n.text(report.language, "issue.title", count=count, noun=noun, project=project, sha=report.sha)


def issue_body(report: AuditReport, decide: Sequence[Mapping[str, Any]], by_id: Mapping[str, Any]) -> str:
    def say(key: str) -> str:
        return i18n.text(report.language, key)

    lines = [say("issue.intro"), ""]
    for v in decide:
        f = by_id[v["id"]]
        lines += [
            f"- [ ] **{v['id']}** {f['title']} ({v['severity']}), `{f['file']}:{f['lines']}`",
            f"  {f['why']}",
            f"  *{say('issue.fix')}:* {f['fix']}",
            f"  *{say('issue.why')}:* {v['reason']}",
        ]
    footer = i18n.text(report.language, "issue.footer", sha=report.sha, engine=report.engine)
    return "\n".join([*lines, "", f"<sub>{footer}</sub>"])


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
    if report.deliveries or report.issue_url:
        out += ["", "## Delivered", ""]
        for d in report.deliveries:
            out.append(
                f"- {d['title']} ({', '.join(d['ids'])}): **{d.get('status', 'pending')}** "
                + (d.get("url") or d.get("note") or "")[:300]
            )
        if report.issue_url:
            out.append(f"- Decisions issue: {report.issue_url}")
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
