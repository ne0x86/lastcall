"""The lastcall command line.

lastcall login <account> --engine claude-code   # on the host: the CLI's own login, inside the sandbox
lastcall quota <account> --engine claude-code   # where the supervisor socket is: read the account's quota
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from lastcall import engines
from lastcall.accounts import Account, ensure_volume, login_argv, read_quota
from lastcall.quota import QuotaError, Reading, SpendPolicy, plan
from lastcall.sandbox import Sandbox, SandboxError
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
    args = parser.parse_args(argv)
    try:
        account = Account(args.account, args.engine)
    except ValueError as e:
        parser.error(str(e))
    return login(account) if args.command == "login" else quota(account)


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
        reset = datetime.fromtimestamp(w.resets_at).strftime("%a %d %H:%M") if w.resets_at else "unknown"
        lines.append(f"  {w.name:<5} {w.used_pct:5.1f}% used, resets {reset} ({w.role})")
    lines.append(f"  paid usage: {'ACTIVE, nothing will be spent' if reading.paid_usage else 'off'}")
    p = plan(reading, SpendPolicy(), DEFAULT_RATE, now)
    lines.append(f"  burst: {'yes' if p.burst else 'no'} ({p.reason})")
    return "\n".join(lines)
