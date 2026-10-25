"""One audit run from start to end, as the command line and the service both run it.

The run's folder lives in the data volume (`runs/<repo>-<engine>-<time>`) and holds the clone, the context pack, a
working copy per pull request and the report. The store keeps the run, its findings and their pull requests.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from lastcall import engines, environment, git, pack, scanners
from lastcall.accounts import Account, read_quota
from lastcall.engines.base import Engine, Invocation, StepResult, StepSpec
from lastcall.environment import TOOL_ENV, Environment
from lastcall.github import GitHub, GitHubForge
from lastcall.quota import Gate, QuotaError, Reading, SpendPolicy, gate
from lastcall.recipes.audit import (
    AuditReport,
    Peer,
    QuotaCheck,
    StepRecord,
    StepRunner,
    Verifier,
    deliver,
    markdown,
    models,
    run_audit,
)
from lastcall.sandbox import Sandbox, SandboxError
from lastcall.store import Store
from lastcall.tiers import defaults

if TYPE_CHECKING:
    from lastcall.notify import Notifier

VERIFY_TIMEOUT = 25 * 60.0
Log = Callable[[str], None]


@dataclass(frozen=True)
class Instance:
    """What a run needs from the instance."""

    box: Sandbox
    client: GitHub
    store: Store
    notify: Notifier | None = None


@dataclass(frozen=True)
class Options:
    language: str = "English"
    max_prs: int = 3
    report_only: bool = False
    policy: SpendPolicy = field(default_factory=SpendPolicy)
    wait: bool = False  # wait for a full throttle window to reset (the service), or stop the run (the CLI)
    trigger: str = "manual"


def agent_hooks(
    box: Sandbox,
    account: Account,
    policy: SpendPolicy,
    wait: bool = False,
    log: Log = print,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[StepRunner, QuotaCheck]:
    """How a recipe runs agent steps for this account: in the sandbox, behind the quota gate."""

    def run_step(engine: Engine, step: StepSpec, workspace: Path) -> StepResult:
        return box.run(engine, step, workspace=workspace, account_volume=account.volume)

    def check_quota(models: Sequence[str]) -> tuple[Gate, Reading | None]:
        while True:
            try:
                reading = read_quota(account, box, time.time())
            except (QuotaError, SandboxError) as e:
                return Gate(False, f"no quota reading: {e}"), None
            verdict = gate(reading, policy, time.time(), models)
            if verdict.ok or not wait or verdict.wait_seconds <= 0:
                return verdict, reading
            log(f"{account.id}: {verdict.reason}; waiting {verdict.wait_seconds / 60:.0f} min for it to reset")
            sleep(verdict.wait_seconds)

    return run_step, check_quota


def audit_models(engine: str) -> list[str]:
    """The models that an audit on this engine's accounts uses: no burst starts while one of them is out of room."""
    return models(defaults(engine))


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


def save(run_dir: Path, report: AuditReport) -> str:
    (run_dir / "report.json").write_text(report.to_json(), encoding="utf-8")
    text = markdown(report)
    (run_dir / "report.md").write_text(text, encoding="utf-8")
    return text


def audit_repo(
    inst: Instance,
    repo: str,
    account: Account,
    opts: Options,
    on_step: Callable[[StepRecord], None] | None = None,
    log: Log = print,
    peers: Sequence[tuple[Account, SpendPolicy]] = (),
) -> tuple[Path, AuditReport]:
    """Clone, pack, find and judge; then, unless the run is report-only, implement, verify and deliver. `peers`
    are other accounts whose reviewers take part, each behind its own quota gate (fan-out)."""
    token = inst.client.repository_token(repo)
    meta = inst.client.repository(token, repo)
    run_dir = inst.box.data_root / "runs" / f"{meta['name']}-{account.engine}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir.mkdir(parents=True)
    inst.store.start_run(run_dir.name, repo, account.id, opts.trigger, [p.id for p, _ in peers])
    try:
        git.clone(repo, run_dir / "clone", token, str(meta["default_branch"]))
        own = environment.settings(run_dir / "clone")  # a wrong .lastcall.yml stops the run before any spending
        env = environment.detect(run_dir / "clone", own)
        log(f"Checks of {repo} run in {env.name} ({env.source})" + (f"; {env.note}" if env.note else ""))
        log(f"Scanning {repo}: gitleaks, osv-scanner, semgrep and hadolint, in the sandbox.")
        found = scanners.scan(inst.box, account.engine, run_dir / "clone")
        if own and own.focus:
            found[FOCUS] = own.focus
        context = pack.build(run_dir / "clone", run_dir, repo, extra=found, exclude=own.exclude if own else ())
        log(
            f"Auditing {repo} @ {context.sha} with {account.id} ({account.engine}): {context.files} files, "
            f"~{context.tokens / 1000:.0f}k tokens of code. Run: {run_dir.name}"
        )
        run_step, check_quota = agent_hooks(inst.box, account, opts.policy, opts.wait, log)
        crew = [
            Peer(p.id, engines.get(p.engine), defaults(p.engine), *agent_hooks(inst.box, p, policy, opts.wait, log))
            for p, policy in peers
        ]
        if crew:
            log(f"Reviewed also by {', '.join(p.name for p in crew)}; {account.id} judges and implements.")
        report = run_audit(
            repo=repo,
            pack=context,
            engine=engines.get(account.engine),
            tiers=defaults(account.engine),
            run_step=run_step,
            check_quota=check_quota,
            language=opts.language,
            max_prs=opts.max_prs,
            earlier=inst.store.earlier(repo),
            on_step=on_step,
            peers=crew,
        )
        report.environment = f"{env.name} ({env.source})"
        inst.store.record_findings(run_dir.name, report)
        save(run_dir, report)
        if not opts.report_only and report.status == "ok" and report.prs:
            deliver_report(inst, run_dir, report, account, opts, None, on_step, log)
    except Exception as e:
        inst.store.finish_run(run_dir.name, "failed", None)
        if inst.notify:
            inst.notify.notify("failed", repo=repo, reason=f"{type(e).__name__}: {e}"[:400])
        raise
    inst.store.finish_run(run_dir.name, report.status, report.sha)
    if inst.notify and report.status in ("failed", "partial"):
        error = next((s.error for s in reversed(report.steps) if s.error), None)
        reason = report.stopped or error or f"run {run_dir.name}"
        inst.notify.notify("failed", report.status, repo=repo, reason=reason[:400])
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
    run_step, check_quota = agent_hooks(inst.box, account, opts.policy, opts.wait, log)
    clone = run_dir / "clone"
    env = environment.detect(clone, environment.settings(clone)) if clone.is_dir() else environment.RUNNER
    opened = {d.get("url") for d in report.deliveries if d.get("status") == "opened"}
    issue = report.issue_url
    deliver(
        report=report,
        run_dir=run_dir,
        engine=engines.get(account.engine),
        tiers=defaults(account.engine),
        run_step=run_step,
        check_quota=check_quota,
        verify=verifier(inst.box, account.engine, env),
        forge=forge,
        max_prs=max_prs,
        on_step=on_step,
        environment=env,
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
