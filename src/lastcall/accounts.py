"""Accounts: where capacity comes from. Each account keeps its CLI login in its own Docker volume.

The owner signs in with the CLI's own interactive login, run in a container with the same isolation as a step. The
CLI stores the login in the account volume; lastcall never reads or copies it (DESIGN.md, section 3). Quota readers
run in the sandbox with that volume mounted.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from lastcall import engines
from lastcall.engines.base import Engine, QuotaProbe
from lastcall.quota import QuotaError, Reading
from lastcall.sandbox import Sandbox
from lastcall.supervisor import RUNNER_UID, Config

ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PROBE_TIMEOUT = 90.0
PROBE_WORKSPACE = "runs/probes"  # an empty workspace: quota probes do not look at any project
ENGINE_LABEL = "lastcall.engine"


@dataclass(frozen=True)
class Account:
    id: str
    engine: str

    def __post_init__(self) -> None:
        if not ACCOUNT_ID.fullmatch(self.id):
            raise ValueError(f"account ids use lowercase letters, digits and dashes (40 at most): {self.id!r}")
        if self.engine not in engines.BUILTIN:
            raise ValueError(f"unknown engine {self.engine!r}; available: {', '.join(sorted(engines.BUILTIN))}")

    @property
    def volume(self) -> str:
        return f"lastcall-account-{self.id}"


def login_argv(account: Account, engine: Engine, config: Config | None = None) -> list[str]:
    """`docker run` for the CLI's official interactive login, with the isolation of a step and a terminal."""
    cfg = config or Config()
    return [
        *("docker", "run", "--rm", "-it", "--name", f"lastcall-login-{account.id}", "--network", cfg.network),
        *("--user", f"{RUNNER_UID}:{RUNNER_UID}", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"),
        *("-e", f"HTTPS_PROXY={cfg.proxy}", "-e", f"HTTP_PROXY={cfg.proxy}", "-e", "NO_PROXY=localhost,127.0.0.1"),
        *("-e", f"{engine.home_env}={engine.home}"),
        *("--mount", f"type=volume,src={account.volume},dst={engine.home}"),
        cfg.images[engine.name],
        *engine.login,
    ]


def ensure_volume(account: Account) -> None:
    """Create the account volume, labelled with its engine. Refuse a volume that belongs to another engine."""
    found = subprocess.run(
        ["docker", "volume", "inspect", "--format", f'{{{{index .Labels "{ENGINE_LABEL}"}}}}', account.volume],
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode != 0:
        subprocess.run(
            ["docker", "volume", "create", "--label", f"{ENGINE_LABEL}={account.engine}", account.volume],
            capture_output=True,
            check=True,
        )
        return
    label = found.stdout.strip()
    if label not in ("", "<no value>", account.engine):
        raise ValueError(f"{account.volume} holds a {label} login, not {account.engine}: use another account id")


def read_quota(account: Account, sandbox: Sandbox, now: float) -> Reading:
    """The account's quota, read in the sandbox with its login. Raises QuotaError when it cannot be read."""
    engine = engines.get(account.engine)
    if not isinstance(engine, QuotaProbe):
        raise QuotaError(f"{account.engine} has no quota reader")
    workspace = sandbox.data_root / PROBE_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    outcome = sandbox.call(
        engine.name,
        engine.quota_invocation(),
        lines.append,
        workspace=workspace,
        account_volume=account.volume,
        home=engine.home,
        timeout=PROBE_TIMEOUT,
    )
    if outcome.timed_out:
        raise QuotaError(f"no quota answer in {PROBE_TIMEOUT:g} s")
    try:
        return engine.quota_reading(account.id, lines, now)
    except QuotaError as e:
        detail = outcome.stderr_tail.strip() if not lines else ""  # stderr explains only a probe that said nothing
        raise QuotaError(f"{e}: {detail[-400:]}" if detail else str(e)) from None
