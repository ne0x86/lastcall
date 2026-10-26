"""The lastcall command line.

lastcall login <account> --engine <engine>      # on the host: the CLI's own login, inside the sandbox
lastcall account add <account> --engine <engine> [--language Spanish] [--reserve 10]
lastcall account add <account> --engine opencode --provider openrouter --model <model> --budget 20  # an API key
lastcall project add <owner/name> [--weight 1] [--language Spanish] [--inactive]
lastcall quota <account>                        # read an account's quota through the supervisor
lastcall github setup --owner <login>           # create the instance's GitHub App (manifest flow)
lastcall github repos                           # installed repositories, with lastcall's suggestions
lastcall audit <owner/name> --account <account> # audit, then pull requests; --account again: fan-out
lastcall deliver <run> --account <account>      # the pull requests of a report-only audit
lastcall improve <owner/name> --account <account> # an improvement plan; later runs build the ideas you tick
lastcall issues <owner/name> --account <account>  # work now on the issues labelled `lastcall`
lastcall tests <owner/name> --account <account>   # tests for the behavior that no test checks
lastcall sync                                   # what the owner did with lastcall's pull requests and issues
lastcall status                                 # accounts, projects and what happened to their findings
lastcall serve [--port 8080]                    # the service, which bursts on its own, and its web UI
lastcall web [--port 8080]                      # only the web UI
lastcall password reset                         # forget the web UI's password: the log shows a new setup code
lastcall pause | resume                         # stop or allow the service's bursts
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from lastcall import engines, projects, web
from lastcall.accounts import (
    Account,
    check_spending,
    ensure_volume,
    login_argv,
    policies,
    read_quota,
    tier_models,
)
from lastcall.engines.base import Keyed
from lastcall.github import App, GitHub, GitHubError
from lastcall.github_setup import SetupServer, run_setup
from lastcall.notify import Notifier
from lastcall.quota import KINDS, QuotaError, Reading, SpendPolicy, account_rate, plan
from lastcall.recipes.audit import AuditReport
from lastcall.recipes.steps import StepRecord
from lastcall.runs import (
    Instance,
    Options,
    audit_models,
    audit_repo,
    deliver_report,
    improve_repo,
    issues_repo,
    save,
    tests_repo,
)
from lastcall.sandbox import Sandbox, SandboxError
from lastcall.secretstore import SecretStore
from lastcall.service import DEFAULT_RATE as SERVICE_RATE
from lastcall.service import RATE_WINDOW, Service
from lastcall.store import Store, import_runs
from lastcall.suggest import Suggestion
from lastcall.supervisor import Config
from lastcall.sync import sync
from lastcall.web import auth

DEFAULT_RATE = 2.0  # % of the week per hour until the rate is learned from readings
RUN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PROVIDERS = sorted(
    {p.name for make in engines.BUILTIN.values() if isinstance(e := make(), Keyed) for p in e.providers}
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lastcall", description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    login_cmd = commands.add_parser("login", help="sign in an account with its CLI's own login (run it on the host)")
    login_cmd.add_argument("account", help="account id: lowercase letters, digits and dashes")
    login_cmd.add_argument("--engine", required=True, choices=sorted(engines.BUILTIN))

    acc = commands.add_parser("account", help="accounts lastcall may spend").add_subparsers(
        dest="action", required=True
    )
    acc_add = acc.add_parser(
        "add", help="add or change an account: sign in a subscription with `login`; an API key needs no login"
    )
    acc_add.add_argument("account")
    acc_add.add_argument("--engine", required=True, choices=sorted(engines.BUILTIN))
    acc_add.add_argument("--language", default="English", help="language of what it writes (default English)")
    acc_add.add_argument("--reserve", type=float, default=10.0, help="%% of every week kept for you (default 10)")
    acc_add.add_argument(
        "--provider",
        choices=PROVIDERS,
        help="for an engine on API keys: whose API it spends, or ollama for local models",
    )
    acc_add.add_argument(
        "--key-file", help="the API key: a file that holds it, or - to read it from standard input (default: asked)"
    )
    acc_add.add_argument("--model", help="the model of every step, as the provider names it")
    acc_add.add_argument("--review-model", help="a cheaper model for the reviewers (default: --model)")
    acc_add.add_argument(
        "--policy",
        choices=KINDS,
        help="when it spends: before-reset (subscriptions), budget (API keys), schedule or manual",
    )
    acc_add.add_argument("--budget", type=float, help="USD it may spend per calendar month (API keys)")
    acc_add.add_argument("--schedule", help='policy schedule: when it bursts, as cron in UTC, such as "* 1-6 * * *"')
    acc_add.add_argument("--base-url", help="another address of the provider's API, such as a local model server")
    acc.add_parser("list", help="the accounts")

    proj = commands.add_parser("project", help="repositories lastcall works on").add_subparsers(
        dest="action", required=True
    )
    proj_add = proj.add_parser("add", help="add or change a project (the GitHub App must be installed on it)")
    proj_add.add_argument("repo", help="owner/name")
    proj_add.add_argument("--weight", type=float, default=1.0, help="0 never, 2 twice the priority (default 1)")
    proj_add.add_argument("--language", default=None, help="language for this project, over the account's")
    proj_add.add_argument("--inactive", action="store_true", help="keep it, but do not audit it")
    proj.add_parser("list", help="the projects")

    quota_cmd = commands.add_parser("quota", help="read an account's quota through the supervisor")
    quota_cmd.add_argument("account")

    gh = commands.add_parser("github", help="the instance's GitHub App").add_subparsers(dest="action", required=True)
    setup = gh.add_parser("setup", help="create the GitHub App with GitHub's manifest flow")
    setup.add_argument("--owner", required=True, help="GitHub account or organization that will own the App")
    setup.add_argument("--org", action="store_true", help="the owner is an organization")
    setup.add_argument("--port", type=int, default=8765, help="port of the setup page (default 8765)")
    gh.add_parser("repos", help="installed repositories, with lastcall's suggestions")

    audit_cmd = commands.add_parser("audit", help="audit a repository and open pull requests for what it finds")
    audit_cmd.add_argument("repo", help="owner/name: the GitHub App must be installed on it")
    audit_cmd.add_argument("--max-prs", type=int, default=3, help="pull requests the judge may plan (default 3)")
    audit_cmd.add_argument("--report-only", action="store_true", help="stop after the judge: change nothing")
    deliver_cmd = commands.add_parser("deliver", help="implement, verify and open the pull requests of a report")
    deliver_cmd.add_argument("run", help="the run folder name that `audit --report-only` printed")
    deliver_cmd.add_argument("--max-prs", type=int, default=None, help="open only the first N planned pull requests")
    audit_cmd.add_argument(
        "--account",
        required=True,
        action="append",
        help="the account whose quota the steps spend; repeat it to add the reviewers of other accounts (fan-out): "
        "the first one judges and implements",
    )
    deliver_cmd.add_argument("--account", required=True, help="the account whose quota the steps spend")
    improve_cmd = commands.add_parser(
        "improve", help="an improvement plan, an issue with a checkbox per idea: later runs build the ideas you tick"
    )
    improve_cmd.add_argument("repo", help="owner/name: the GitHub App must be installed on it")
    improve_cmd.add_argument("--account", required=True, help="the account whose quota the steps spend")
    issues_cmd = commands.add_parser(
        "issues", help="work now on the issues labelled `lastcall`: a pull request each, or a comment saying why not"
    )
    issues_cmd.add_argument("repo", help="owner/name: the GitHub App must be installed on it")
    issues_cmd.add_argument("--account", required=True, help="the account whose quota the steps spend")
    tests_cmd = commands.add_parser(
        "tests", help="tests for the behavior that no test checks, one pull request each; they change only tests"
    )
    tests_cmd.add_argument("repo", help="owner/name: the GitHub App must be installed on it")
    tests_cmd.add_argument("--account", required=True, help="the account whose quota the steps spend")

    commands.add_parser("sync", help="what the owner did with lastcall's pull requests and issues")
    commands.add_parser("status", help="accounts, projects and what happened to their findings")
    serve_cmd = commands.add_parser(
        "serve", help="run the service and its web UI: it spends quota that would go unused, on active projects"
    )
    web_cmd = commands.add_parser("web", help="serve only the web UI")
    for cmd in (serve_cmd, web_cmd):
        cmd.add_argument(
            "--host", default=os.environ.get("LASTCALL_WEB_HOST", "127.0.0.1"), help="address to listen on"
        )
        cmd.add_argument("--port", type=int, default=int(os.environ.get("LASTCALL_WEB_PORT", "8080")))
    password = commands.add_parser("password", help="the web UI's password").add_subparsers(
        dest="action", required=True
    )
    password.add_parser("reset", help="forget it and end every session; the service's log shows a new setup code")
    commands.add_parser("pause", help="no new bursts (a running one finishes; the audits you order still start)")
    commands.add_parser("resume", help="the service may burst again")
    import_cmd = commands.add_parser("import", help="bring reports of runs made before the store into it")
    import_cmd.add_argument("--account", required=True, help="the account those runs spent")

    args = parser.parse_args(argv)
    try:
        return dispatch(args)
    except ValueError as e:
        parser.error(str(e))


def dispatch(args: argparse.Namespace) -> int:
    match args.command, getattr(args, "action", None):
        case "login", _:
            if not engines.get(args.engine).login:
                raise ValueError(
                    f"{args.engine} has no login: it runs on an API key or a local model. Add the account with "
                    f"`lastcall account add {args.account} --engine {args.engine} --provider ...`"
                )
            return login(Account(args.account, args.engine))
        case "account", "add":
            return add_account(args)
        case "account", "list":
            for a in store().accounts():
                engine = f"{a.engine}/{a.provider}" if a.provider else a.engine
                limit = f"budget {a.budget_usd:g} USD a month" if a.budget_usd else f"reserve {a.reserve_pct:g}%"
                print(
                    f"{a.id:<20} {engine:<20} {a.language:<10} {a.policy:<12} {limit}"
                    + (f"  schedule {a.schedule}" if a.policy == "schedule" else "")
                    + ("" if a.enabled else "  (disabled)")
                )
            return 0
        case "project", "add":
            store().add_project(args.repo, not args.inactive, args.weight, args.language)
            print(f"Project {args.repo}: {'inactive' if args.inactive else 'active'}, weight {args.weight:g}.")
            return 0
        case "project", "list":
            for p in store().projects():
                state = "active" if p.active and p.weight > 0 else "inactive"
                print(f"{p.repo:<40} {state:<8} weight {p.weight:g}  {p.language or ''}")
            return 0
        case "quota", _:
            return quota(account(args.account))
        case "github", "setup":
            return github_setup(args.owner, args.org, args.port)
        case "github", _:
            return github_repos()
        case "audit", _:
            lead, *others = [account(a) for a in dict.fromkeys(args.account)]
            return audit(args.repo, lead, args.max_prs, args.report_only, others)
        case "deliver", _:
            return deliver_run(args.run, account(args.account), args.max_prs)
        case "improve", _:
            return improve(args.repo, account(args.account))
        case "issues", _:
            return issues(args.repo, account(args.account))
        case "tests", _:
            return tests(args.repo, account(args.account))
        case "sync", _:
            client = github_client()
            if client is None:
                return 1
            print(f"{sync(store(), client, log)} changes.")
            return 0
        case "status", _:
            print(status(store()))
            return 0
        case "serve", _:
            return serve(args.host, args.port, scheduler=True)
        case "web", _:
            return serve(args.host, args.port, scheduler=False)
        case "password", "reset":
            auth.Owner(secret_store()).reset()
            store().drop_sessions()
            print("Password forgotten and sessions ended. Open the web UI: the service's log shows a setup code.")
            return 0
        case "pause" | "resume", _:
            store().set_setting("paused", "yes" if args.command == "pause" else "no")
            print("Paused: no new bursts." if args.command == "pause" else "Resumed.")
            return 0
        case "import", _:
            return import_reports(account(args.account))
    raise ValueError(f"unknown command {args.command}")


def store() -> Store:
    return Store(Path(os.environ.get("LASTCALL_STATE", "/state")) / "lastcall.db")


def account(account_id: str) -> Account:
    found = store().account(account_id)
    if found is None:
        raise ValueError(f"no account {account_id!r}: add it with `lastcall account add {account_id} --engine ...`")
    return found.account()


def add_account(args: argparse.Namespace) -> int:
    models = tier_models(args.model or "", args.review_model or "")
    acc = Account(args.account, args.engine, args.provider or "", args.base_url or "", models, args.budget)
    policy = args.policy or policies(acc)[0]
    schedule = args.schedule or ""
    check_spending(acc, policy, schedule)
    secrets = secret_store()
    if acc.needs_key():
        key = read_key(args.key_file, ask=secrets.get(acc.key_name) is None)
        if key:
            secrets.set(acc.key_name, key)
        elif secrets.get(acc.key_name) is None:
            raise ValueError("give the API key with --key-file: a file that holds it, or - for standard input")
    store().add_account(
        acc.id,
        acc.engine,
        args.language,
        args.reserve,
        provider=acc.provider,
        base_url=acc.base_url,
        models=models,
        policy=policy,
        budget_usd=acc.budget_usd,
        schedule=schedule,
    )
    if acc.keyed:
        print(f"Account {acc.id} ({acc.engine} on {acc.provider}), policy {policy}. It needs no login.")
    else:
        print(f"Account {acc.id} ({acc.engine}). Sign it in with: lastcall login {acc.id} --engine {acc.engine}")
    return 0


def read_key(path: str | None, ask: bool) -> str | None:
    """An API key from a file, from standard input (`-`), or typed at a prompt that does not show it; None when
    there is none to read."""
    if path == "-":
        key = sys.stdin.readline()
    elif path:
        key = Path(path).read_text(encoding="utf-8")
    elif ask and sys.stdin.isatty():
        key = getpass.getpass("API key (it is not shown): ")
    else:
        return None
    if not key.strip():
        raise ValueError("the API key is empty")
    return key.strip()


def secret_store() -> SecretStore:
    return SecretStore(Path(os.environ.get("LASTCALL_SECRETS", "/secrets")))


def instance(client: GitHub, st: Store | None = None) -> Instance:
    st, secrets = st or store(), secret_store()
    return Instance(sandbox(), client, st, Notifier(secrets, st, log=log), secrets)


def sandbox() -> Sandbox:
    return Sandbox(
        Path(os.environ.get("LASTCALL_SOCKET", "/run/lastcall/supervisor.sock")),
        Path(os.environ.get("LASTCALL_DATA_ROOT", "/data")),
    )


def github_client() -> GitHub | None:
    app = App.load(secret_store())
    if app is None:
        print("No GitHub App yet: run `lastcall github setup --owner <login>` first.", file=sys.stderr)
        return None
    return GitHub(app)


def options(acc: Account, repo: str, max_prs: int = 3, report_only: bool = False) -> Options:
    """The account's language and reserve, and the project's language when it has one."""
    st = store()
    row = next(a for a in st.accounts() if a.id == acc.id)
    project = st.project(repo)
    language = (project.language if project and project.language else None) or row.language
    return Options(language=language, max_prs=max_prs, report_only=report_only, policy=row.spend_policy())


def progress(s: StepRecord) -> None:
    state = "ok" if s.ok else f"FAILED: {(s.error or '')[:200]}"
    print(f"  {s.role}: {s.model} {s.effort or ''}, {s.seconds:g} s, {s.tool_uses} tools, {state}", flush=True)


def log(message: str) -> None:
    print(message, flush=True)


def audit(repo: str, acc: Account, max_prs: int, report_only: bool, peers: Sequence[Account] = ()) -> int:
    client = github_client()
    if client is None:
        return 1
    inst = instance(client)
    spending = {a.id: a.spend_policy() for a in inst.store.accounts()}
    crew = [(p, spending[p.id]) for p in peers]
    try:
        run_dir, report = audit_repo(
            inst, repo, acc, options(acc, repo, max_prs=max_prs, report_only=report_only), progress, log, crew
        )
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n" + (run_dir / "report.md").read_text(encoding="utf-8"))
    return 1 if report.status == "failed" else 0


def improve(repo: str, acc: Account) -> int:
    client = github_client()
    if client is None:
        return 1
    try:
        run_dir, report = improve_repo(instance(client), repo, acc, options(acc, repo), progress, log)
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n" + (run_dir / "report.md").read_text(encoding="utf-8"))
    return 1 if report.status == "failed" else 0


def issues(repo: str, acc: Account) -> int:
    client = github_client()
    if client is None:
        return 1
    try:
        run_dir, report = issues_repo(instance(client), repo, acc, options(acc, repo), progress, log)
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n" + (run_dir / "report.md").read_text(encoding="utf-8"))
    return 1 if report.status == "failed" else 0


def tests(repo: str, acc: Account) -> int:
    client = github_client()
    if client is None:
        return 1
    try:
        run_dir, report = tests_repo(instance(client), repo, acc, options(acc, repo), progress, log)
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n" + (run_dir / "report.md").read_text(encoding="utf-8"))
    return 1 if report.status == "failed" else 0


def deliver_run(run: str, acc: Account, max_prs: int | None) -> int:
    client = github_client()
    if client is None:
        return 1
    inst = instance(client)
    run_dir = inst.box.data_root / "runs" / run
    if not RUN_NAME.fullmatch(run) or not (run_dir / "report.json").exists():
        print(f"No report in run {run!r}.", file=sys.stderr)
        return 1
    report = AuditReport.from_json((run_dir / "report.json").read_text(encoding="utf-8"))
    import_runs(inst.store, [(run, report, acc.id)])  # a report from before the store, if it is one
    try:
        deliver_report(inst, run_dir, report, acc, options(acc, report.repo), max_prs, progress, log)
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    print("\n" + save(run_dir, report))
    return 0


def serve(host: str, port: int, scheduler: bool) -> int:
    st, secrets = store(), secret_store()
    origins = [o.strip() for o in os.environ.get("LASTCALL_WEB_ORIGIN", "").split(",") if o.strip()]
    app = web.create_app(
        st,
        secrets,
        log=log,
        trust_proxy=os.environ.get("LASTCALL_TRUST_PROXY") == "1",
        origins=origins,
        notifier=Notifier(secrets, st, log=log),
        read_quota=lambda account: read_quota(account, sandbox(), time.time(), st, secrets),
    )
    if scheduler:
        threading.Thread(target=schedule, args=(st, secrets), name="service", daemon=True).start()
    web.serve(app, host, port, log)
    return 0


def schedule(st: Store, secrets: SecretStore) -> None:
    """The service, as soon as the GitHub App exists. Until then only the web UI works, and the heartbeat says the
    service is alive."""
    if App.load(secrets) is None:
        log("Waiting for the GitHub App: create it with `lastcall github setup --owner <login>`")
    while (app := App.load(secrets)) is None:
        st.set_setting("heartbeat", f"{time.time():.0f}")
        time.sleep(60)
    Service(instance(GitHub(app), st), log=log).serve()


def import_reports(acc: Account) -> int:
    runs = sandbox().data_root / "runs"
    found = []
    for path in sorted(runs.glob("*/report.json")):
        found.append((path.parent.name, AuditReport.from_json(path.read_text(encoding="utf-8")), acc.id))
    print(f"Imported {import_runs(store(), found)} of {len(found)} runs.")
    return 0


def status(st: Store) -> str:
    now = time.time()
    lines = ["New runs: paused (lastcall resume)" if st.setting("paused") == "yes" else "New runs: allowed"]
    lines.append("Accounts")
    for a in st.accounts():
        readings = st.readings(a.id, now - 7 * 86_400)
        last = readings[-1] if readings else None
        quota_text = (
            ", ".join(f"{w.label} {w.used_pct:g}%" for w in last.windows)
            + f" ({(now - last.taken_at) / 60:.0f} min ago)"
            if last
            else "no reading yet"
        )
        limit = f"budget {a.budget_usd:g} USD a month" if a.budget_usd else f"reserve {a.reserve_pct:g}%"
        lines.append(f"  {a.id} ({a.engine}, {a.language}, {a.policy}, {limit}): {quota_text}")
        if last:
            policy = a.spend_policy()
            rate = account_rate(st.readings(a.id, now - RATE_WINDOW), last, policy, SERVICE_RATE)
            try:
                needs = audit_models(a.account())
            except ValueError:
                needs = []
            next_burst = plan(last, policy, rate, now, needs)
            when = "now" if next_burst.burst else "no"
            pace = "" if a.keyed else f"; {rate:.1f}% of the week per hour"
            lines.append(f"    burst: {when} ({next_burst.reason}{pace})")
    lines.append("Projects")
    for p in st.projects():
        counts = st.statuses(p.repo)
        findings = ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) or "no findings yet"
        state = "active" if p.active and p.weight > 0 else "inactive"
        lines.append(f"  {p.repo} ({state}, weight {p.weight:g}): {findings}; {len(st.open_prs(p.repo))} open PRs")
    return "\n".join(lines)


def github_setup(owner: str, org: bool, port: int) -> int:
    try:
        server = SetupServer(("0.0.0.0", port), secret_store(), owner, org)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"Ready: open http://127.0.0.1:{port}/ in your browser now (the page answers from this moment on).\n"
        "Keep this command running until GitHub brings you back to that page.",
        flush=True,
    )
    app = run_setup(server)
    print(f"App {app.name} created. Install it on the repositories lastcall may work on:\n  {app.install_url}")
    return 0


def github_repos() -> int:
    client = github_client()
    if client is None:
        return 1
    try:
        found = projects.refresh(store(), client, time.time())
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    if not found:
        print(f"{client.app.name} is not installed on any repository yet. Install it:\n  {client.app.install_url}")
        return 0
    print(describe_suggestions(found))
    return 0


def describe_suggestions(found: list[Suggestion]) -> str:
    lines = [f"{'':2}{'score':>5}  {'repository':<36} {'language':<12} {'code':>7}  why"]
    for s in sorted(found, key=lambda s: (-s.score, s.repo)):
        mark = "* " if s.suggested else "  "
        size = f"~{s.code_tokens / 1000:.0f}k" if s.code_tokens else "-"
        lines.append(f"{mark}{s.score:>5}  {s.repo:<36} {s.language or '-':<12} {size:>7}  {'; '.join(s.reasons)}")
    lines.append("* suggested. Code size in tokens. Add one with: lastcall project add <owner/name>")
    return "\n".join(lines)


def login(acc: Account) -> int:
    if shutil.which("docker") is None:
        print("lastcall login runs Docker: install it, or run this command on the Docker host.", file=sys.stderr)
        return 2
    cfg = Config()
    net = subprocess.run(["docker", "network", "inspect", cfg.network], capture_output=True, check=False)
    if net.returncode != 0:
        print(
            f"The sandbox is not running (no network {cfg.network}): start it with `docker compose up -d`.",
            file=sys.stderr,
        )
        return 2
    try:
        ensure_volume(acc)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"Signing in {acc.id} with {acc.engine}'s own login. The login stays in the volume "
        f"{acc.volume}; lastcall never reads it.\n",
        flush=True,
    )
    return subprocess.run(login_argv(acc, engines.get(acc.engine), cfg), check=False).returncode


def quota(acc: Account) -> int:
    now, st = time.time(), store()
    try:
        reading = read_quota(acc, sandbox(), now, st, secret_store())
    except (QuotaError, SandboxError) as e:
        print(f"{acc.id}: {e}", file=sys.stderr)
        return 1
    st.record_reading(reading)
    row = st.account(acc.id)
    print(describe(reading, now, audit_models(acc), row.spend_policy() if row else SpendPolicy()))
    return 0


def describe(reading: Reading, now: float, models: Sequence[str] = (), policy: SpendPolicy | None = None) -> str:
    lines = [f"{reading.account} ({reading.fidelity})"]
    width = max([5, *(len(w.label) for w in reading.windows)])
    for w in reading.windows:
        reset = datetime.fromtimestamp(w.resets_at, UTC).strftime("%a %d %H:%M UTC") if w.resets_at else "unknown"
        lines.append(f"  {w.label:<{width}} {w.used_pct:5.1f}% used, resets {reset} ({w.role})")
    lines.append(f"  paid usage: {'ACTIVE, nothing will be spent' if reading.paid_usage else 'off'}")
    p = plan(reading, policy or SpendPolicy(), DEFAULT_RATE, now, models)
    lines.append(f"  burst: {'yes' if p.burst else 'no'} ({p.reason})")
    return "\n".join(lines)
