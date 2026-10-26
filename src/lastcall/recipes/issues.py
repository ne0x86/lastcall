"""The issues recipe (DESIGN.md, section 9): issue → fix → verify → deliver.

The owner labels an issue `lastcall`. In its next run on the project, lastcall gives the issue, with the comments of
people who may write to the repository, to an implementer, and builds the change like any other
(lastcall.recipes.build): its own clone, the check outside the agent, one more attempt, the secret scan. The pull
request closes the issue when it is merged. When the change cannot be made, lastcall says why on the issue and takes
the label off; the owner adds it again once the issue says what was missing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lastcall import i18n
from lastcall.engines.base import Engine
from lastcall.environment import RUNNER, Environment
from lastcall.recipes import prompt, render
from lastcall.recipes.build import Forge, Job, Verifier, build
from lastcall.recipes.steps import QuotaCheck, StepRecord, StepRunner, Steps, table

LABEL = "lastcall"
MAX_BODY = 8_000  # characters of an issue's text that the implementer reads
MAX_COMMENTS = 20
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # whose comments count: people who may write to the repository


@dataclass
class IssuesReport:
    repo: str
    sha: str
    engine: str
    language: str
    status: str = "ok"  # ok | partial (stopped by a gate) | failed
    stopped: str | None = None
    steps: list[StepRecord] = field(default_factory=list)
    deliveries: list[dict[str, Any]] = field(default_factory=list)  # one per issue, once tried
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> IssuesReport:
        data = json.loads(text)
        data["steps"] = [StepRecord(**s) for s in data.get("steps") or []]
        return cls(**data)


def comments(found: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """The comments that count, as the implementer reads them: from people who may write, the latest ones."""
    trusted = [c for c in found if c.get("author_association") in TRUSTED]
    return [
        {"from": str((c.get("user") or {}).get("login")), "text": str(c.get("body") or "")[:2_000]}
        for c in trusted[-MAX_COMMENTS:]
    ]


def build_issues(
    *,
    report: IssuesReport,
    tickets: Sequence[Mapping[str, Any]],
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
) -> IssuesReport:
    """The labelled issues, the oldest first, one pull request each. Each ticket holds the issue's `number`, `url`,
    `title`, `body` and `comments` (lastcall.recipes.issues.comments), and `row`, its id in the store."""
    steps = Steps(report, engine, tiers, run_step, check_quota, shared, on_step)
    project, stamp = report.repo.split("/")[-1], time.strftime("%Y%m%d-%H%M")
    for n, ticket in enumerate(tickets[:max_prs], 1):

        def ask(branch: str, check: str, ticket: Mapping[str, Any] = ticket) -> str:
            return render(
                prompt("fixer.md"),
                project=project,
                branch=branch,
                url=str(ticket["url"]),
                title=str(ticket["title"]),
                body=str(ticket.get("body") or "")[:MAX_BODY] or "(no description)",
                comments=json.dumps(list(ticket.get("comments") or []), ensure_ascii=False, indent=1),
                check=check,
                language=report.language,
            )

        def footer(checked: str, model: str, effort: str | None, number: int = int(ticket["number"])) -> str:
            values = {"number": str(number), "model": model, "effort": effort or "-"}
            return i18n.text(report.language, "pr.ticket.footer", checked=checked, **values)

        number = int(ticket["number"])
        job = Job(str(ticket["title"]), ask, f"Fixes #{number}", footer)
        record = build(
            job,
            {"title": ticket["title"], "ids": [f"#{number}"], "row": ticket["row"], "number": number},
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


def why(language: str, record: Mapping[str, Any]) -> str:
    """Why an issue could not be done, for the comment lastcall leaves on it."""
    status = str(record.get("status"))
    if status == "not_done":
        return str(record.get("note") or "").strip() or i18n.text(language, "ticket.unchanged")
    key = {"red": "ticket.red", "secrets": "ticket.secrets"}.get(status, "ticket.unchanged")
    return i18n.text(language, key)


def markdown(report: IssuesReport) -> str:
    """The report for the owner: what became of each labelled issue, and what each step cost."""
    out = [f"# Labelled issues of {report.repo} @ {report.sha} ({report.engine})", ""]
    out.append(f"Status: **{report.status}**" + (f" ({report.stopped})" if report.stopped else ""))
    out += [f"- {note}" for note in report.notes]
    if report.deliveries:
        out += ["", "## Issues", ""]
        for d in report.deliveries:
            detail = (d.get("url") or d.get("note") or "")[:300]
            out.append(f"- {', '.join(d['ids'])} {d['title']}: **{d.get('status', 'pending')}** {detail}")
    return "\n".join([*out, "", "## Steps", "", *table(report.steps), ""])
