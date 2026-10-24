"""The lastcall command line.

lastcall login <account> --engine claude-code   # on the host: the CLI's own login, inside the sandbox
lastcall quota <account> --engine claude-code   # where the supervisor socket is: read the account's quota
lastcall github setup --owner <login>           # create the instance's GitHub App (manifest flow)
lastcall github repos                           # installed repositories, with lastcall's suggestions
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from lastcall import engines
from lastcall.accounts import Account, ensure_volume, login_argv, read_quota
from lastcall.github import App, GitHub, GitHubError
from lastcall.github_setup import SetupServer, run_setup
from lastcall.quota import QuotaError, Reading, SpendPolicy, plan
from lastcall.sandbox import Sandbox, SandboxError
from lastcall.secretstore import SecretStore
from lastcall.suggest import Suggestion, suggest
from lastcall.supervisor import Config

DEFAULT_RATE = 2.0  # % of the week per hour until the rate is learned from readings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lastcall", description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("login", "sign in an account with its CLI's own login, inside the sandbox (run it on the host)"),
        ("quota", "read an account's quota through the supervisor"),
    ):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("account", help="account id: lowercase letters, digits and dashes")
        cmd.add_argument("--engine", required=True, choices=sorted(engines.BUILTIN))
    gh = commands.add_parser("github", help="the instance's GitHub App").add_subparsers(dest="action", required=True)
    setup = gh.add_parser("setup", help="create the GitHub App with GitHub's manifest flow")
    setup.add_argument("--owner", required=True, help="GitHub account or organization that will own the App")
    setup.add_argument("--org", action="store_true", help="the owner is an organization")
    setup.add_argument("--port", type=int, default=8765, help="port of the setup page (default 8765)")
    gh.add_parser("repos", help="installed repositories, with lastcall's suggestions")
    args = parser.parse_args(argv)
    if args.command == "github":
        return github_setup(args.owner, args.org, args.port) if args.action == "setup" else github_repos()
    try:
        account = Account(args.account, args.engine)
    except ValueError as e:
        parser.error(str(e))
    return login(account) if args.command == "login" else quota(account)


def secret_store() -> SecretStore:
    return SecretStore(Path(os.environ.get("LASTCALL_SECRETS", "/secrets")))


def github_setup(owner: str, org: bool, port: int) -> int:
    try:
        server = SetupServer(("0.0.0.0", port), secret_store(), owner, org)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(f"Open http://127.0.0.1:{port}/ in your browser and create the App. Waiting for GitHub...", flush=True)
    app = run_setup(server)
    print(f"App {app.name} created. Install it on the repositories lastcall may work on:\n  {app.install_url}")
    return 0


def github_repos() -> int:
    app = App.load(secret_store())
    if app is None:
        print("No GitHub App yet: run `lastcall github setup --owner <login>` first.", file=sys.stderr)
        return 1
    client, now, found = GitHub(app), time.time(), []
    try:
        installations = client.installations()
        for inst in installations:
            token = client.installation_token(int(inst["id"]))
            for repo in client.repositories(token):
                files, truncated = client.files(token, repo["full_name"], repo["default_branch"])
                found.append(suggest(repo, files, now, truncated))
    except GitHubError as e:
        print(e, file=sys.stderr)
        return 1
    if not installations:
        print(f"{app.name} is not installed anywhere yet. Install it:\n  {app.install_url}")
        return 0
    print(describe_suggestions(found))
    return 0


def describe_suggestions(found: list[Suggestion]) -> str:
    lines = [f"{'':2}{'score':>5}  {'repository':<36} {'language':<12} {'code':>7}  why"]
    for s in sorted(found, key=lambda s: (-s.score, s.repo)):
        mark = "* " if s.suggested else "  "
        size = f"~{s.code_tokens / 1000:.0f}k" if s.code_tokens else "-"
        lines.append(f"{mark}{s.score:>5}  {s.repo:<36} {s.language or '-':<12} {size:>7}  {'; '.join(s.reasons)}")
    lines.append("* suggested. Code size in tokens.")
    return "\n".join(lines)


def login(account: Account) -> int:
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
        ensure_volume(account)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"Signing in {account.id} with {account.engine}'s own login. The login stays in the volume "
        f"{account.volume}; lastcall never reads it.\n",
        flush=True,
    )
    return subprocess.run(login_argv(account, engines.get(account.engine), cfg), check=False).returncode


def quota(account: Account) -> int:
    sandbox = Sandbox(
        Path(os.environ.get("LASTCALL_SOCKET", "/run/lastcall/supervisor.sock")),
        Path(os.environ.get("LASTCALL_DATA_ROOT", "/data")),
    )
    now = time.time()
    try:
        reading = read_quota(account, sandbox, now)
    except (QuotaError, SandboxError) as e:
        print(f"{account.id}: {e}", file=sys.stderr)
        return 1
    print(describe(reading, now))
    return 0


def describe(reading: Reading, now: float) -> str:
    lines = [f"{reading.account} ({reading.fidelity})"]
    for w in reading.windows:
        reset = datetime.fromtimestamp(w.resets_at, UTC).strftime("%a %d %H:%M UTC") if w.resets_at else "unknown"
        lines.append(f"  {w.name:<5} {w.used_pct:5.1f}% used, resets {reset} ({w.role})")
    lines.append(f"  paid usage: {'ACTIVE, nothing will be spent' if reading.paid_usage else 'off'}")
    p = plan(reading, SpendPolicy(), DEFAULT_RATE, now)
    lines.append(f"  burst: {'yes' if p.burst else 'no'} ({p.reason})")
    return "\n".join(lines)
