"""Accounts: where capacity comes from. An account runs on its CLI's own login, kept in its own Docker volume, or on
an API key or a local model server (a keyed engine such as opencode).

The owner signs in with the CLI's own interactive login, run in a container with the same isolation as a step. The
CLI stores the login in the account volume; lastcall never reads or copies it (DESIGN.md, section 3). Quota readers
run in the sandbox with that volume mounted.

An API key is different: lastcall holds it in its secret store, and each step gets it as a read-only file in /ctx.
Such an account has no quota reader of its provider. lastcall keeps a ledger of what its steps cost, and its reading
is this month's spending against the monthly budget that the owner sets.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from lastcall import cron, engines
from lastcall.engines.base import Engine, Keyed, Provider, QuotaProbe
from lastcall.quota import Kind, QuotaError, Reading, ledger_reading, month
from lastcall.sandbox import Sandbox
from lastcall.secretstore import SecretStore
from lastcall.supervisor import RUNNER_UID, Config
from lastcall.tiers import TIERS, defaults

if TYPE_CHECKING:
    from lastcall.store import Store

ACCOUNT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
PROBE_TIMEOUT = 90.0
PROBE_WORKSPACE = "runs/probes"  # an empty workspace: quota probes do not look at any project
ENGINE_LABEL = "lastcall.engine"


@dataclass(frozen=True)
class Account:
    id: str
    engine: str
    provider: str = ""  # an API's provider, or a local model server: the account runs on a key, not on a CLI login
    base_url: str = ""  # another address for the provider's API, such as a model server on the sandbox network
    models: Mapping[str, str] = field(default_factory=dict)  # tier -> model, over the engine's defaults
    budget_usd: float | None = None  # what its steps may cost per calendar month (UTC)

    def __post_init__(self) -> None:
        if not ACCOUNT_ID.fullmatch(self.id):
            raise ValueError(f"account ids use lowercase letters, digits and dashes (40 at most): {self.id!r}")
        if self.engine not in engines.BUILTIN:
            raise ValueError(f"unknown engine {self.engine!r}; available: {', '.join(sorted(engines.BUILTIN))}")
        engine = engines.get(self.engine)
        if self.provider:
            if not isinstance(engine, Keyed):
                raise ValueError(f"{self.engine} runs on its CLI's own login, not on an API key")
            if self.provider not in [p.name for p in engine.providers]:
                names = ", ".join(p.name for p in engine.providers)
                raise ValueError(f"unknown provider {self.provider!r} for {self.engine}; available: {names}")
        elif not engine.login:
            raise ValueError(f"{self.engine} runs on an API key or a local model: give the account a provider")
        if set(self.models) - set(TIERS):
            raise ValueError(f"models are given per tier: {', '.join(TIERS)}")
        if self.budget_usd is not None and self.budget_usd <= 0:
            raise ValueError("a monthly budget is more than 0 USD")
        self.tiers()  # every tier has a model

    @property
    def volume(self) -> str:
        return f"lastcall-account-{self.id}"

    @property
    def keyed(self) -> bool:
        """It runs on an API key or a local model server, which lastcall reaches with no CLI login."""
        return bool(self.provider)

    @property
    def key_name(self) -> str:
        """The secret that holds its API key."""
        return f"key-{self.id}"

    def needs_key(self) -> bool:
        source = self.source()
        return source is not None and bool(source.keys)

    def source(self) -> Provider | None:
        """Its provider, as the engine describes it; None for an account on a CLI login."""
        engine = engines.get(self.engine)
        if not self.provider or not isinstance(engine, Keyed):
            return None
        return next(p for p in engine.providers if p.name == self.provider)

    def tiers(self) -> dict[str, tuple[str, str | None]]:
        """tier -> (model, effort): the engine's defaults, and the account's own models over them. A model of its
        own keeps the effort of the default for that tier."""
        try:
            found = defaults(self.engine)
        except ValueError:  # an engine with no default models, such as opencode: the account names them
            found = {}
        for tier, model in self.models.items():
            found[tier] = (model, found[tier][1] if tier in found else None)
        missing = [t for t in TIERS if t not in found]
        if missing:
            raise ValueError(f"account {self.id} needs a model for: {', '.join(missing)}")
        return found


def tier_models(model: str, review: str = "") -> dict[str, str]:
    """An account's own models from what the owner gives: one model for every step, and maybe a cheaper one for the
    reviewers (the balanced tier, and the fast one)."""
    models: dict[str, str] = dict.fromkeys(TIERS, model) if model else {}
    if review:
        models |= {"fast": review, "balanced": review}
    return models


def engine_for(account: Account, secrets: SecretStore | None = None) -> Engine:
    """The account's engine, on its API key or local model server when it has a provider."""
    engine = engines.get(account.engine)
    if not account.keyed or not isinstance(engine, Keyed):
        return engine
    key = secrets.get(account.key_name) if secrets is not None and account.needs_key() else None
    return engine.with_key(account.provider, key, account.base_url or None)


def policies(account: Account) -> tuple[Kind, ...]:
    """The spend policies that fit the account, the default first. A subscription spends what would expire at the
    reset, an API key spends a monthly budget, and a local model server costs nothing, so it follows a schedule."""
    if not account.keyed:
        return ("before-reset", "manual")
    if account.needs_key():
        return ("budget", "schedule", "manual")
    return ("schedule", "manual")


def check_spending(account: Account, policy: str, schedule: str = "") -> None:
    """ValueError unless the policy fits the account: an API key always has a monthly budget, whatever its policy,
    and a schedule is a valid cron expression."""
    fits = policies(account)
    if policy not in fits:
        raise ValueError(f"policy {policy!r} does not fit account {account.id}; it takes: {', '.join(fits)}")
    if account.needs_key() and not account.budget_usd:
        raise ValueError(f"account {account.id} runs on an API key: give it a monthly budget in USD")
    if policy == "schedule":
        cron.parse(schedule)


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


def read_quota(
    account: Account,
    sandbox: Sandbox | None,
    now: float,
    store: Store | None = None,
    secrets: SecretStore | None = None,
) -> Reading:
    """The account's quota, read in the sandbox with its login. For an account on an API key or a local model,
    nothing runs: the reading comes from the ledger in the store. Raises QuotaError when it cannot be read, and for a
    provider that needs a key while the account has none."""
    if account.keyed:
        if account.needs_key() and (secrets is None or secrets.get(account.key_name) is None):
            raise QuotaError("no API key: give the account one on the Accounts page")
        if store is None:
            raise QuotaError("no ledger to read")
        return ledger_reading(account.id, account.budget_usd, store.spent(account.id, month(now)[0]), now)
    engine = engines.get(account.engine)
    if not isinstance(engine, QuotaProbe):
        raise QuotaError(f"{account.engine} has no quota reader")
    if sandbox is None:
        raise QuotaError("the sandbox is not available here")
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
