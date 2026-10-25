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
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from lastcall import git, i18n
from lastcall.engines.base import Engine
from lastcall.environment import RUNNER, ConfigError, Environment
from lastcall.pack import Pack
from lastcall.recipes import front_matter, prompt, render
from lastcall.recipes.build import Forge, Job, Verifier, build
from lastcall.recipes.steps import QuotaCheck, StepRecord, StepRunner, Steps, table
from lastcall.secretscan import scan
from lastcall.tiers import TIERS

PROJECT_REVIEWERS = ".lastcall/reviewers"  # a project's own reviewers, one Markdown file each
MAX_PROJECT_REVIEWERS = 4  # each one is a step of every run
MAX_REVIEWER_BYTES = 8_000
REVIEWER_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
REVIEWER_PREFIX = re.compile(r"[A-Z]{1,3}")  # finding ids: A1, A2...


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


def project_reviewers(root: Path) -> list[Reviewer]:
    """The project's own reviewers, from `.lastcall/reviewers/*.md` in its repository, with the front matter of the
    built-in ones. Raises ConfigError when one is wrong: the run stops before it spends."""
    folder = root / PROJECT_REVIEWERS
    if not folder.is_dir():
        return []
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ConfigError(f"{PROJECT_REVIEWERS} is a link out of the repository")
    paths = sorted(folder.glob("*.md"))
    if len(paths) > MAX_PROJECT_REVIEWERS:
        raise ConfigError(f"{PROJECT_REVIEWERS} has {len(paths)} reviewers, and a run takes {MAX_PROJECT_REVIEWERS}")
    found = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if git.repo_file(root, rel) is None:
            raise ConfigError(f"{rel} is a link that does not lead to a file of the repository")
        if path.stat().st_size > MAX_REVIEWER_BYTES:
            raise ConfigError(f"{rel} is over {MAX_REVIEWER_BYTES // 1000} KB: a focus takes a few paragraphs")
        try:
            meta, body = front_matter(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ConfigError(f"{rel}: {e}") from e
        name, prefix, tier = meta.get("name", ""), meta.get("prefix", ""), meta.get("tier", "")
        if not REVIEWER_NAME.fullmatch(name):
            raise ConfigError(f"{rel}: `name` takes lowercase letters, digits and dashes, such as accessibility")
        if not REVIEWER_PREFIX.fullmatch(prefix):
            raise ConfigError(f"{rel}: `prefix` takes one to three capital letters, such as A")
        if tier not in TIERS:
            raise ConfigError(f"{rel}: `tier` is one of {', '.join(TIERS)}")
        if not body:
            raise ConfigError(f"{rel}: the reviewer's focus goes after the front matter")
        found.append(Reviewer(name, prefix, tier, body))
    return found


def team(root: Path, chosen: Sequence[str] = ()) -> list[Reviewer]:
    """The reviewers of a run: the built-in ones, then the project's own; only those that `.lastcall.yml` names, when
    it names some. Raises ConfigError for a wrong reviewer, a name or prefix used twice, or an unknown name."""
    everyone = [*reviewers(), *project_reviewers(root)]
    for kind, values in (("name", [rv.name for rv in everyone]), ("prefix", [rv.prefix for rv in everyone])):
        twice = sorted({v for v in values if values.count(v) > 1})
        if twice:
            raise ConfigError(f"each reviewer needs its own {kind}, and two have {', '.join(twice)}")
    if not chosen:
        return everyone
    by_name = {rv.name: rv for rv in everyone}
    unknown = [name for name in chosen if name not in by_name]
    if unknown:
        raise ConfigError(f"`reviewers` names {', '.join(unknown)}, and the reviewers are {', '.join(by_name)}")
    return [by_name[name] for name in dict.fromkeys(chosen)]


def models(tiers: Mapping[str, tuple[str, str | None]], team: Sequence[Reviewer] | None = None) -> list[str]:
    """The models an audit uses: its reviewers', then the judge's and the implementers' (deep)."""
    crew = team if team is not None else reviewers()
    return list(dict.fromkeys([*(tiers[rv.tier][0] for rv in crew), tiers["deep"][0]]))


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
    environment: str = ""  # where the checks run: an image, or the runner, and what decided it
    peers: list[str] = field(default_factory=list)  # other accounts whose reviewers took part (fan-out)
    notes: list[str] = field(default_factory=list)  # what happened to them, such as a peer stopped by its quota

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> AuditReport:
        data = json.loads(text)
        data["steps"] = [StepRecord(**s) for s in data.get("steps") or []]
        return cls(**data)


@dataclass(frozen=True)
class Peer:
    """Another account whose reviewers take part in a run (fan-out): its own engine, tiers and quota gate. Its
    findings and steps carry `@<name>`. Only the run's own account judges and implements."""

    name: str
    engine: Engine
    tiers: Mapping[str, tuple[str, str | None]]
    run_step: StepRunner
    check_quota: QuotaCheck


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
    peers: Sequence[Peer] = (),
) -> AuditReport:
    """First half: find and judge. With peers, every account's reviewers work at the same time, one after another
    within each account, on the same pack; one judge, this account's, weighs all their findings."""
    report = AuditReport(repo, pack.sha, engine.name, language, peers=[p.name for p in peers])
    steps = Steps(report, engine, tiers, run_step, check_quota, pack.path, on_step)
    project = repo.split("/")[-1]
    crew = list(team if team is not None else reviewers())

    def review(steps: Steps, mark: str) -> list[dict[str, Any]] | None:
        """The findings of one account's reviewers; None when this run's own gate closes. The first reviewer's gate
        checks every model the account needs in the run, the judge's too for the run's own account."""
        found: list[dict[str, Any]] = []
        needed = models(steps.tiers, crew) if not mark else [steps.tiers[rv.tier][0] for rv in crew]
        for n, rv in enumerate(crew):
            text = render(prompt("reviewer.md"), role=rv.name, project=project, focus=rv.focus, language=language)
            then = needed if n == 0 else ()
            res = steps.run(f"reviewer:{rv.name}{mark}", rv.tier, text, "findings", pack.src, then=then)
            if res is None:
                return found if mark else None
            if res.ok:  # a reviewer that fails costs its findings, not the run
                for i, finding in enumerate((res.output or {}).get("findings") or [], 1):
                    found.append({"id": f"{rv.prefix}{i}{mark}", "reviewer": rv.name, **finding})
        return found

    if peers:
        others = [
            Steps(report, p.engine, p.tiers, p.run_step, p.check_quota, pack.path, on_step, p.name) for p in peers
        ]
        with ThreadPoolExecutor(max_workers=1 + len(peers)) as pool:
            own = pool.submit(review, steps, "")
            theirs = [pool.submit(review, s, f"@{s.peer}") for s in others]
            mine, extra = own.result(), [f for t in theirs for f in t.result() or []]
    else:
        mine, extra = review(steps, ""), []
    if mine is None:
        return report
    report.findings = mine + extra
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
    environment: Environment = RUNNER,
) -> AuditReport:
    """Second half: implement each planned pull request, verify it outside the agent, in the project's environment,
    and open it. Pull requests already opened for this report are not opened again."""
    steps = Steps(report, engine, tiers, run_step, check_quota, run_dir / "shared.md", on_step)
    by_id = {f["id"]: f for f in report.findings}
    verdict = {v["id"]: v for v in report.verdicts}
    opened = {d["title"] for d in report.deliveries if d["status"] == "opened"}
    project, stamp = report.repo.split("/")[-1], time.strftime("%Y%m%d-%H%M")

    for n, pr in enumerate(report.prs[:max_prs] if max_prs else report.prs, 1):
        if pr["title"] in opened:
            continue
        covered = [dict(by_id[i], judge=verdict.get(i)) for i in pr["ids"]]

        def ask(branch: str, check: str, pr: Mapping[str, Any] = pr, covered: list[Any] = covered) -> str:
            return render(
                prompt("implementer.md"),
                project=project,
                branch=branch,
                title=pr["title"],
                risk=pr["risk"],
                plan=pr["plan"],
                findings=json.dumps(covered, ensure_ascii=False, indent=1),
                check=check,
                language=report.language,
            )

        def footer(checked: str, model: str, effort: str | None, ids: Sequence[str] = pr["ids"]) -> str:
            return pr_footer(report, ids, model, effort, checked)

        job = Job(pr["title"], ask, f"Findings: {', '.join(pr['ids'])}", footer)
        record = build(
            job,
            {"title": pr["title"], "ids": pr["ids"]},
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
    if report.peers:
        out.append(f"Also reviewed by: {', '.join(report.peers)}. One judge weighed every finding.")
    out += [f"- {note}" for note in report.notes]
    if report.summary:
        out += ["", report.summary]
    if report.deliveries or report.issue_url:
        out += ["", "## Delivered", ""]
        for d in report.deliveries:
            out.append(
                f"- {d['title']} ({', '.join(d['ids'])}): **{d.get('status', 'pending')}**"
                + (f" after {d['attempts']} attempts" if d.get("attempts", 1) > 1 else "")
                + " "
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
        *table(report.steps),
    ]
    return "\n".join(out) + "\n"
