"""One run from start to end, an audit, tests, dependency upgrades, an improvement or the labelled issues, as the
command line and the service both run it.

The run's folder lives in the data volume (`runs/<repo>-<engine>-<time>`, or `<repo>-<recipe>-<engine>-<time>`) and
holds the clone, the context pack, a working copy per pull request and the report. The store keeps the run, its
findings, ideas or issues, and their pull requests.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lastcall import environment, git, i18n, pack, scanners, signals
from lastcall.accounts import Account, engine_for, read_quota
from lastcall.engines.base import Engine, Invocation, StepResult, StepSpec
from lastcall.environment import TOOL_ENV, Environment
from lastcall.github import GitHub, GitHubForge
from lastcall.quota import Gate, QuotaError, Reading, SpendPolicy, gate
from lastcall.recipes.audit import (
    AUDIT,
    AuditReport,
    Peer,
    Recipe,
    deliver,
    markdown,
    models,
    run_audit,
    team,
)
from lastcall.recipes.build import Verifier
from lastcall.recipes.deps import DEPS, UPGRADES
from lastcall.recipes.improve import ImproveReport, build_ideas, publish, run_improve
from lastcall.recipes.improve import markdown as improve_markdown
from lastcall.recipes.issues import LABEL, IssuesReport, build_issues, comments, why
from lastcall.recipes.issues import markdown as issues_markdown
from lastcall.recipes.steps import QuotaCheck, StepRecord, StepRunner
from lastcall.recipes.tests import TESTER, TESTS
from lastcall.sandbox import Sandbox, SandboxError
from lastcall.secretstore import SecretStore
from lastcall.store import Store

if TYPE_CHECKING:
    from lastcall.notify import Notifier

VERIFY_TIMEOUT = 25 * 60.0
RECIPES = {r.name: r for r in (AUDIT, TESTS, DEPS)}  # the recipes of the audit's shape, by the name in reports
DOING = {AUDIT.name: "Auditing", TESTS.name: "Looking for untested behavior in", DEPS.name: "Choosing upgrades for"}
Log = Callable[[str], None]


@dataclass(frozen=True)
class Instance:
    """What a run needs from the instance."""

    box: Sandbox
    client: GitHub
    store: Store
    notify: Notifier | None = None
    secrets: SecretStore | None = None  # where the API keys of accounts on keys are, and the error tracker's token
    fetch: signals.Fetch = signals.fetch_json  # how lastcall reads the error tracker


@dataclass(frozen=True)
class Options:
    language: str = "English"
    max_prs: int = 3
    report_only: bool = False
    policy: SpendPolicy = field(default_factory=SpendPolicy)
    wait: bool = False  # wait for a full throttle window to reset (the service), or stop the run (the CLI)
    trigger: str = "manual"


def agent_hooks(
    inst: Instance,
    account: Account,
    policy: SpendPolicy,
    wait: bool = False,
    log: Log = print,
    sleep: Callable[[float], None] = time.sleep,
    run: str = "",
) -> tuple[StepRunner, QuotaCheck]:
    """How a recipe runs agent steps for this account: in the sandbox, behind the quota gate. The steps of an account
    on an API key or a local model get no account volume, and the ledger keeps what each one cost. When a step on an
    API key used tokens that the engine could not price, the budget cannot hold, and the next gate stops the run."""
    unpriced: list[str] = []

    def run_step(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
        if not account.keyed:
            return inst.box.run(engine, step, workspace=workspace, account_volume=account.volume)
        res = inst.box.run(engine, step, workspace=workspace)
        inst.store.record_spend(account.id, res.cost_usd or 0.0, step.role, run)
        if account.needs_key() and not res.cost_usd and _used_tokens(res) and not step.model.endswith(":free"):
            unpriced.append(step.model)
        return res

    def check_quota(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        if unpriced:
            reason = f"{unpriced[0]} used tokens that have no price, so the budget cannot hold: choose a priced model"
            return Gate(False, reason), None
        while True:
            try:
                reading = read_quota(account, inst.box, time.time(), inst.store, inst.secrets)
            except (QuotaError, SandboxError) as e:
                return Gate(False, f"no quota reading: {e}"), None
            verdict = gate(reading, policy, time.time(), models)
            if verdict.ok or not wait or verdict.wait_seconds <= 0:
                return verdict, reading
            log(f"{account.id}: {verdict.reason}; waiting {verdict.wait_seconds / 60:.0f} min for it to reset")
            sleep(verdict.wait_seconds)

    return run_step, check_quota


def _used_tokens(res: StepResult) -> bool:
    return isinstance(res.usage, Mapping) and any(int(v or 0) > 0 for v in res.usage.values())


def audit_models(account: Account) -> list[str]:
    """The models that an audit on this account uses: no burst starts while one of them is out of room."""
    return models(account.tiers())


FOCUS = "What matters most to the owner (.lastcall.yml)"


def verifier(box: Sandbox, engine: str, env: Environment = environment.RUNNER) -> Verifier:
    """Runs the check command on a clean copy of the change, outside the agent: in the project's environment image
    after its setup, or in the runner image."""

    def verify(work: Path, command: str) -> tuple[bool, str]:
        copy = work.with_name(f"{work.name}-verify")
        git.remove(copy)
        for rel in git.files(work):
            target = copy / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / rel, target, follow_symlinks=False)
        copy.mkdir(parents=True, exist_ok=True)
        tail: list[str] = []

        def keep(line: str) -> None:
            tail.append(line)
            del tail[:-40]

        invocation = Invocation(("sh", "-c", env.script(command)), env=TOOL_ENV if env.image else {})
        outcome = box.call(
            engine, invocation, keep, workspace=copy, writable=True, timeout=VERIFY_TIMEOUT, image=env.image
        )
        output = "\n".join([*tail, outcome.stderr_tail.strip()]).strip()
        return outcome.exit_code == 0 and not outcome.timed_out, output

    return verify


def save(run_dir: Path, report: AuditReport | ImproveReport | IssuesReport) -> str:
    (run_dir / "report.json").write_text(report.to_json(), encoding="utf-8")
    if isinstance(report, ImproveReport):
        text = improve_markdown(report)
    elif isinstance(report, IssuesReport):
        text = issues_markdown(report)
    else:
        text = markdown(report)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


def tell(inst: Instance, run: str, report: AuditReport | ImproveReport | IssuesReport, prefix: str = "") -> None:
    """Tell the owner when a run failed or stopped early, and why."""
    if inst.notify and report.status in ("failed", "partial"):
        error = next((s.error for s in reversed(report.steps) if s.error), None)
        reason = report.stopped or error or f"run {run}"
        inst.notify.notify("failed", prefix + report.status, repo=report.repo, reason=reason[:400])


def audit_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
    peers: Sequence[tuple[Account, SpendPolicy]] = (),
    recipe: Recipe = AUDIT,
) -> tuple[Path, AuditReport]:
    """Clone, pack, find and judge; then, unless the run is report-only, implement, verify and deliver. `peers`
    are other accounts whose reviewers take part, each behind its own quota gate (fan-out). With the tests recipe,
    the one reviewer looks for behavior that no test checks, and the pull requests may change only tests."""
    token = inst.client.repository_token(repo)
    meta = inst.client.repository(token, repo)
    kind = "" if recipe is AUDIT else f"-{recipe.name}"
    run_dir = inst.box.data_root / "runs" / f"{meta['name']}{kind}-{account.engine}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True)
    inst.store.start_run(run_dir.name, repo, account.id, opts.trigger, [p.id for p, _ in peers], recipe.name)
    try:
        git.clone(repo, run_dir / "clone", token, str(meta["default_branch"]))
        own = environment.settings(run_dir / "clone")  # a wrong .lastcall.yml stops the run before any spending
        # and so does a wrong reviewer of its own; the tests and the upgrades have their one reviewer each
        panel = {TESTS.name: [TESTER], DEPS.name: [UPGRADES]}.get(recipe.name) or team(
            run_dir / "clone", own.reviewers if own else ()
        )
        env = environment.detect(run_dir / "clone", own)
        log(f"Checks of {repo} run in {env.name} ({env.source})" + (f"; {env.note}" if env.note else ""))
        log(f"Reviewers: {', '.join(rv.name for rv in panel)}")
        found: dict[str, str] = {}
        if recipe is AUDIT:  # what the scanners report is for the audit; the tests do not need it
            log(f"Scanning {repo}: gitleaks, osv-scanner, semgrep and hadolint, in the sandbox.")
            found = scanners.scan(inst.box, account.engine, run_dir / "clone")
        elif recipe is DEPS:
            log(f"Reading the dependencies of {repo}: osv-scanner and deps.dev, in the sandbox.")
            found = scanners.dependencies(inst.box, account.engine, run_dir / "clone")
        if recipe is not DEPS:  # what fails in production, for the audits and the tests
            found |= production_errors(inst, repo, run_dir / "clone", log)
        if own and own.focus:
            found[FOCUS] = own.focus
        context = pack.build(run_dir / "clone", run_dir, repo, extra=found, exclude=own.exclude if own else ())
        log(
            f"{DOING[recipe.name]} {repo} @ {context.sha} with "
            f"{account.id} ({account.engine}): {context.files} files, ~{context.tokens / 1000:.0f}k tokens of code. "
            f"Run: {run_dir.name}"
        )
        run_step, check_quota = agent_hooks(inst, account, opts.policy, opts.wait, log, run=run_dir.name)
        crew = [
            Peer(
                p.id,
                engine_for(p, inst.secrets),
                p.tiers(),
                *agent_hooks(inst, p, policy, opts.wait, log, run=run_dir.name),
            )
            for p, policy in peers
        ]
        if crew:
            log(f"Reviewed also by {', '.join(p.name for p in crew)}; {account.id} judges and implements.")
        report = run_audit(
            repo=repo,
            pack=context,
            engine=engine_for(account, inst.secrets),
            tiers=account.tiers(),
            run_step=run_step,
            check_quota=check_quota,
            language=opts.language,
            max_prs=opts.max_prs,
            earlier=inst.store.earlier(repo, recipe.name),
            team=panel,
            on_step=on_step,
            peers=crew,
            recipe=recipe,
        )
        report.environment = f"{env.name} ({env.source})"
        inst.store.record_findings(run_dir.name, report)
        save(run_dir, report)
        if not opts.report_only and report.status == "ok" and report.prs:
            deliver_report(inst, run_dir, report, account, opts, None, on_step, log)
    except Exception as e:
        inst.store.finish_run(run_dir.name, "failed", None)
        if inst.notify:
            key = "failed" if recipe is AUDIT else f"{recipe.name}-failed"
            inst.notify.notify("failed", key, repo=repo, reason=f"{type(e).__name__}: {e}"[:400])
        raise
    inst.store.finish_run(run_dir.name, report.status, report.sha)
    tell(inst, run_dir.name, report, "" if recipe is AUDIT else f"{recipe.name}-")
    return run_dir, report


def production_errors(inst: Instance, repo: str, clone: Path, log: Log = print) -> dict[str, str]:
    """The pack's section on errors in production, when the owner connected an error tracker and gave the project
    its project there. A tracker that cannot be read leaves a note, never a failed run."""
    project = inst.store.project(repo)
    tracker = signals.Tracker.load(inst.secrets) if inst.secrets is not None else None
    if tracker is None or project is None or not project.signals:
        return {}
    log(f"Reading the errors of {repo} in production, from {tracker.url}.")
    try:
        found = signals.errors(tracker, project.signals, git.files(clone), time.time(), inst.fetch)
    except signals.SignalsError as e:
        log(f"{repo}: the errors in production were not read: {e}")
        return {signals.TITLE: f"Not read: {e}"}
    return {signals.TITLE: signals.section(found)}


def tests_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
) -> tuple[Path, AuditReport]:
    """Tests for the behavior of the project that no test checks, one pull request each: the audit's shape with the
    tests' recipe."""
    return audit_repo(inst, repo, account, opts, on_step, log, recipe=TESTS)


def deps_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
) -> tuple[Path, AuditReport]:
    """Dependency upgrades that need more than a new version number, one pull request each: the audit's shape with
    the deps recipe."""
    return audit_repo(inst, repo, account, opts, on_step, log, recipe=DEPS)


def improve_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
) -> tuple[Path, ImproveReport]:
    """An improvement run. When the owner has ticked ideas, it builds them, one pull request each, up to the
    project's `max_prs`. Otherwise it makes a plan: the ideator and the critic, and the plan as an issue with a
    checkbox per idea. While a plan still has ideas to tick, a new one waits."""
    token = inst.client.repository_token(repo)
    meta = inst.client.repository(token, repo)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = inst.box.data_root / "runs" / f"{meta['name']}-improve-{account.engine}-{stamp}"
    run_dir.mkdir(parents=True)
    inst.store.start_run(run_dir.name, repo, account.id, opts.trigger, recipe="improve")
    try:
        git.clone(repo, run_dir / "clone", token, str(meta["default_branch"]))
        wanted = inst.store.wanted_ideas(repo)
        waiting = [p["issue_url"] for p in inst.store.plans() if p["repo"] == repo]
        if wanted or not waiting:
            own = environment.settings(run_dir / "clone")  # a wrong .lastcall.yml stops the run before any spending
            extra = {FOCUS: own.focus} if own and own.focus else {}
            context = pack.build(run_dir / "clone", run_dir, repo, extra=extra, exclude=own.exclude if own else ())
            run_step, check_quota = agent_hooks(inst, account, opts.policy, opts.wait, log, run=run_dir.name)
            if wanted:
                env = environment.detect(run_dir / "clone", own)
                log(f"Building {min(len(wanted), opts.max_prs)} idea(s) for {repo}, checked in {env.name}.")
                report = ImproveReport(repo, context.sha, account.engine, opts.language)
                build_ideas(
                    report=report,
                    wanted=wanted,
                    run_dir=run_dir,
                    shared=context.path,
                    engine=engine_for(account, inst.secrets),
                    tiers=account.tiers(),
                    run_step=run_step,
                    check_quota=check_quota,
                    verify=verifier(inst.box, account.engine, env),
                    forge=GitHubForge.connect(inst.client, repo),
                    environment=env,
                    max_prs=opts.max_prs,
                    on_step=on_step,
                )
                inst.store.record_idea_deliveries(run_dir.name, report)
            else:
                log(f"Ideas for {repo} @ {context.sha} with {account.id} ({account.engine}). Run: {run_dir.name}")
                report = run_improve(
                    repo=repo,
                    pack=context,
                    engine=engine_for(account, inst.secrets),
                    tiers=account.tiers(),
                    run_step=run_step,
                    check_quota=check_quota,
                    language=opts.language,
                    earlier=inst.store.earlier_ideas(repo),
                    on_step=on_step,
                )
                if report.status == "ok":
                    publish(report, GitHubForge.connect(inst.client, repo))
                inst.store.record_ideas(run_dir.name, report)
        else:
            report = ImproveReport(repo, git.head(run_dir / "clone")[:7], account.engine, opts.language)
            report.notes.append(f"The plan {waiting[0]} still has ideas to tick: close it to get a new one.")
            log(f"{repo}: {report.notes[-1]}")
        save(run_dir, report)
    except Exception as e:
        inst.store.finish_run(run_dir.name, "failed", None)
        if inst.notify:
            inst.notify.notify("failed", "improve-failed", repo=repo, reason=f"{type(e).__name__}: {e}"[:400])
        raise
    inst.store.finish_run(run_dir.name, report.status, report.sha)
    if inst.notify:
        for d in report.deliveries:
            if d.get("status") == "opened":
                inst.notify.notify("pr", repo=repo, title=d["title"], url=d["url"])
        if report.issue_url:
            inst.notify.notify("decisions", "plan", repo=repo, count=len(report.plan), url=report.issue_url)
    tell(inst, run_dir.name, report, "improve-")
    return run_dir, report


def issues_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
) -> tuple[Path, IssuesReport]:
    """Work on the issues the owner labelled `lastcall`, the oldest first, up to the project's `max_prs`: a pull
    request that closes each one, or a comment on it that says why not, with the label taken off."""
    token = inst.client.repository_token(repo)
    meta = inst.client.repository(token, repo)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = inst.box.data_root / "runs" / f"{meta['name']}-issues-{account.engine}-{stamp}"
    run_dir.mkdir(parents=True)
    inst.store.start_run(run_dir.name, repo, account.id, opts.trigger, recipe="issues")
    try:
        git.clone(repo, run_dir / "clone", token, str(meta["default_branch"]))
        forge = GitHubForge.connect(inst.client, repo)
        tickets = []
        for ticket in inst.store.waiting_tickets(repo)[: opts.max_prs]:
            issue, found = forge.issue(int(ticket["number"]))
            labels = {str(label.get("name")) for label in issue.get("labels") or []}
            if issue.get("state") == "open" and LABEL in labels:  # still asked for: sync drops the others
                tickets.append({**ticket, "body": issue.get("body") or "", "comments": comments(found)})
        report = IssuesReport(repo, git.head(run_dir / "clone")[:7], account.engine, opts.language)
        if not tickets:
            report.notes.append(f"No issue of {repo} labelled `{LABEL}` waits.")
            log(f"{repo}: {report.notes[-1]}")
        else:
            own = environment.settings(run_dir / "clone")  # a wrong .lastcall.yml stops the run before any spending
            extra = {FOCUS: own.focus} if own and own.focus else {}
            context = pack.build(run_dir / "clone", run_dir, repo, extra=extra, exclude=own.exclude if own else ())
            env = environment.detect(run_dir / "clone", own)
            log(f"Working on {len(tickets)} labelled issue(s) of {repo}, checked in {env.name}.")
            run_step, check_quota = agent_hooks(inst, account, opts.policy, opts.wait, log, run=run_dir.name)
            build_issues(
                report=report,
                tickets=tickets,
                run_dir=run_dir,
                shared=context.path,
                engine=engine_for(account, inst.secrets),
                tiers=account.tiers(),
                run_step=run_step,
                check_quota=check_quota,
                verify=verifier(inst.box, account.engine, env),
                forge=forge,
                environment=env,
                max_prs=opts.max_prs,
                on_step=on_step,
            )
            inst.store.record_ticket_deliveries(run_dir.name, report)
            for d in report.deliveries:
                if d.get("status") not in (None, "opened", "stopped"):  # it could not be done: say why, give it back
                    text = i18n.text(opts.language, "ticket.stuck", why=why(opts.language, d))
                    forge.comment(int(d["number"]), text)
                    forge.unlabel(int(d["number"]), LABEL)
                    inst.store.unlabeled(int(d["row"]))
        save(run_dir, report)
    except Exception as e:
        inst.store.finish_run(run_dir.name, "failed", None)
        if inst.notify:
            inst.notify.notify("failed", "issues-failed", repo=repo, reason=f"{type(e).__name__}: {e}"[:400])
        raise
    inst.store.finish_run(run_dir.name, report.status, report.sha)
    if inst.notify:
        for d in report.deliveries:
            if d.get("status") == "opened":
                inst.notify.notify("pr", repo=repo, title=d["title"], url=d["url"])
    tell(inst, run_dir.name, report, "issues-")
    return run_dir, report


def deliver_report(
    inst: Instance,
    run_dir: Path,
    report: AuditReport,
    account: Account,
    opts: Options,
    max_prs: int | None = None,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
) -> AuditReport:
    forge = GitHubForge.connect(inst.client, report.repo)
    planned = report.prs[:max_prs] if max_prs else report.prs
    log(f"Delivering {len(planned)} pull request(s) for {report.repo} as {forge.author[0]}.")
    run_step, check_quota = agent_hooks(inst, account, opts.policy, opts.wait, log, run=run_dir.name)
    clone = run_dir / "clone"
    env = environment.detect(clone, environment.settings(clone)) if clone.is_dir() else environment.RUNNER
    opened = {d.get("url") for d in report.deliveries if d.get("status") == "opened"}
    issue = report.issue_url
    deliver(
        report=report,
        run_dir=run_dir,
        engine=engine_for(account, inst.secrets),
        tiers=account.tiers(),
        run_step=run_step,
        check_quota=check_quota,
        verify=verifier(inst.box, account.engine, env),
        forge=forge,
        max_prs=max_prs,
        on_step=on_step,
        environment=env,
        recipe=RECIPES.get(report.recipe, AUDIT),
    )
    inst.store.record_deliveries(run_dir.name, report)
    save(run_dir, report)
    if inst.notify:
        for d in report.deliveries:
            if d.get("status") == "opened" and d.get("url") not in opened:
                inst.notify.notify("pr", repo=report.repo, title=d["title"], url=d["url"])
        if report.issue_url and report.issue_url != issue:
            count = sum(v["verdict"] == "valid" and v["decision"] == "issue" for v in report.verdicts)
            inst.notify.notify("decisions", repo=report.repo, count=count, url=report.issue_url)
    return report
