"""Accounts on API keys and local models: what they need, the engine they get, the ledger and the command line."""

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall import cli
from lastcall.accounts import Account, check_spending, engine_for, policies, read_quota, tier_models
from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.engines.opencode import OpenCode
from lastcall.github import GitHub
from lastcall.quota import QuotaError, SpendPolicy
from lastcall.runs import Instance, agent_hooks
from lastcall.sandbox import Sandbox
from lastcall.secretstore import SecretStore
from lastcall.store import Store

MODELS = tier_models("qwen/qwen3-coder")
NOW = datetime(2026, 10, 11, tzinfo=UTC).timestamp()


def openrouter(budget: float | None = 20.0) -> Account:
    return Account("openrouter", "opencode", "openrouter", models=MODELS, budget_usd=budget)


def local() -> Account:
    return Account("local", "opencode", "ollama", models=tier_models("qwen3-coder"))


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "state" / "lastcall.db")


@pytest.fixture
def secrets(tmp_path: Path) -> SecretStore:
    return SecretStore(tmp_path / "secrets")


# what an account on a key needs


def test_an_engine_on_keys_needs_a_provider_and_models() -> None:
    with pytest.raises(ValueError, match="give the account a provider"):
        Account("oc", "opencode")
    with pytest.raises(ValueError, match="unknown provider"):
        Account("oc", "opencode", "nope", models=MODELS)
    with pytest.raises(ValueError, match="needs a model for"):
        Account("oc", "opencode", "openrouter")
    with pytest.raises(ValueError, match="CLI's own login"):
        Account("claude", "claude-code", "openrouter")
    with pytest.raises(ValueError, match="more than 0"):
        openrouter(budget=0)
    assert openrouter().keyed and openrouter().needs_key() and openrouter().key_name == "key-openrouter"
    assert local().keyed and not local().needs_key()
    assert not Account("claude", "claude-code").keyed


def test_models_come_per_tier_and_keep_the_default_effort() -> None:
    assert tier_models("big", "small") == {"fast": "small", "balanced": "small", "deep": "big"}
    assert tier_models("") == {}
    assert openrouter().tiers()["deep"] == ("qwen/qwen3-coder", None)
    claude = Account("claude", "claude-code", models={"deep": "sonnet"})
    assert claude.tiers()["deep"] == ("sonnet", "xhigh")  # the account's own model, the default effort
    assert claude.tiers()["fast"] == ("haiku", "medium")


def test_an_account_chooses_the_model_and_effort_of_each_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claude = Account("max", "claude-code", models={"balanced": "claude-sonnet-5-5"}, efforts={"deep": "max"})
    assert claude.tiers() == {
        "fast": ("haiku", "medium"),
        "balanced": ("claude-sonnet-5-5", "high"),
        "deep": ("opus", "max"),
    }
    with pytest.raises(ValueError, match="claude-code takes the efforts low, medium, high, xhigh, max, not 'ultra'"):
        Account("max", "claude-code", efforts={"deep": "ultra"})
    with pytest.raises(ValueError, match="per tier"):
        Account("max", "claude-code", efforts={"judge": "max"})
    assert Account("chatgpt", "codex", efforts={"fast": "minimal"}).tiers()["fast"] == ("gpt-6-luna", "minimal")
    tuned = Account("local", "opencode", "ollama", models=tier_models("qwen3-coder"), efforts={"deep": "high"})
    assert (tuned.tiers()["deep"], tuned.tiers()["fast"]) == (("qwen3-coder", "high"), ("qwen3-coder", None))

    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    store = Store(tmp_path / "state" / "lastcall.db")
    store.add_account("max", "claude-code")
    store.set_tiers("max", {"balanced": "claude-sonnet-5-5"}, {"deep": "max"})
    row = store.account("max")
    assert row is not None and row.account().tiers() == claude.tiers()
    assert cli.main(["account", "tier", "max", "deep", "--effort", "-"]) == 0  # back to the default
    assert "deep      opus, effort xhigh\n" in capsys.readouterr().out
    assert cli.main(["account", "tier", "max", "balanced", "--model", "-"]) == 0
    assert cli.main(["account", "tier", "max", "fast", "--effort", "low"]) == 0
    assert "fast      haiku, effort low (its own)" in capsys.readouterr().out
    for bad in (["--effort", "ultra"], ["--model", "opus; rm -rf /"]):
        with pytest.raises(SystemExit):
            cli.main(["account", "tier", "max", "deep", *bad])
    row = store.account("max")
    assert row is not None and (row.models, row.efforts) == ({}, {"fast": "low"})


def test_policies_fit_each_kind_of_account() -> None:
    assert policies(Account("claude", "claude-code")) == ("before-reset", "manual")
    assert policies(openrouter()) == ("budget", "schedule", "manual")
    assert policies(local()) == ("schedule", "manual")
    check_spending(openrouter(), "schedule", "* 1-6 * * *")
    with pytest.raises(ValueError, match="monthly budget"):
        check_spending(openrouter(budget=None), "manual")  # a key always has a budget
    with pytest.raises(ValueError, match="does not fit"):
        check_spending(local(), "budget")
    with pytest.raises(ValueError, match="five fields"):
        check_spending(local(), "schedule", "nightly")


# the engine and the reading


def test_the_engine_runs_on_the_accounts_key(secrets: SecretStore) -> None:
    secrets.set("key-openrouter", "sk-or-test")
    engine = engine_for(openrouter(), secrets)
    assert isinstance(engine, OpenCode)
    assert engine.invocation(StepSpec("judge", "qwen/qwen3-coder", "Judge.")).files == {"/ctx/key": "sk-or-test"}
    local_engine = engine_for(local(), secrets)
    assert isinstance(local_engine, OpenCode) and local_engine.key is None
    assert engine_for(Account("claude", "claude-code"), secrets).name == "claude-code"


def test_the_reading_of_a_key_is_the_ledger(store: Store, secrets: SecretStore) -> None:
    with pytest.raises(QuotaError, match="no API key"):
        read_quota(openrouter(), None, NOW, store, secrets)
    secrets.set("key-openrouter", "sk-or-test")
    store.record_spend("openrouter", 4.0, "judge", "run-1", at=NOW - 3600)
    store.record_spend("openrouter", 9.0, "judge", "run-0", at=NOW - 40 * 86_400)  # last month
    store.record_spend("local", 0.0, "judge", "run-2", at=NOW - 60)
    reading = read_quota(openrouter(), None, NOW, store, secrets)
    assert reading.fidelity == "ledger" and reading.windows[0].used_pct == 20.0
    assert read_quota(local(), None, NOW, store).windows == ()
    with pytest.raises(QuotaError, match="sandbox"):
        read_quota(Account("claude", "claude-code"), None, NOW, store)


# the store


def test_accounts_on_keys_round_trip(store: Store) -> None:
    store.add_account(
        "openrouter",
        "opencode",
        "Spanish",
        provider="openrouter",
        models=MODELS,
        policy="budget",
        budget_usd=20.0,
    )
    store.add_account(
        "local", "opencode", provider="ollama", models=local().models, policy="schedule", schedule="* 1-6 * * *"
    )
    row = store.account("openrouter")
    assert row is not None and row.keyed
    assert row.account() == openrouter()
    assert row.spend_policy() == SpendPolicy("budget", 0.0)  # a key keeps no reserve: the budget is the limit
    nights = store.account("local")
    assert nights is not None and nights.spend_policy() == SpendPolicy("schedule", 0.0, cron="* 1-6 * * *")
    store.update_account("local", enabled=True, language="English", reserve_pct=10, policy="manual")
    assert [(a.id, a.policy, a.schedule) for a in store.accounts()] == [
        ("local", "manual", ""),
        ("openrouter", "budget", ""),
    ]


def test_a_database_from_before_v05_gets_the_columns_of_keys(tmp_path: Path) -> None:
    old = sqlite3.connect(tmp_path / "old.db")
    old.execute(
        "CREATE TABLE accounts (id TEXT PRIMARY KEY, engine TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
        "language TEXT NOT NULL DEFAULT 'English', reserve_pct REAL NOT NULL DEFAULT 10, created REAL NOT NULL)"
    )
    old.execute("INSERT INTO accounts (id, engine, created) VALUES ('claude', 'claude-code', 1.0)")
    old.commit()
    old.close()
    (row,) = Store(tmp_path / "old.db").accounts()
    assert (row.provider, row.models, row.policy, row.budget_usd, row.schedule) == ("", {}, "before-reset", None, "")
    assert row.spend_policy() == SpendPolicy(reserve_pct=10)


# the steps of an account on a key


class Box:
    """A sandbox that records how each step was asked to run, and answers with `result`."""

    def __init__(self, result: StepResult) -> None:
        self.result = result
        self.volumes: list[str | None] = []

    def run(
        self, engine: Engine, step: StepSpec, *, workspace: Path, account_volume: str | None = None
    ) -> StepResult:
        self.volumes.append(account_volume)
        return self.result


def hooks(store: Store, secrets: SecretStore, box: Box, account: Account) -> Any:
    inst = Instance(cast(Sandbox, box), cast(GitHub, None), store, secrets=secrets)
    return agent_hooks(inst, account, SpendPolicy("budget", 0.0), run="run-1")


def test_steps_on_a_key_have_no_volume_and_the_ledger_keeps_their_cost(
    store: Store, secrets: SecretStore, tmp_path: Path
) -> None:
    secrets.set("key-openrouter", "sk-or-test")
    box = Box(StepResult(True, {}, cost_usd=0.25, usage={"input": 900, "output": 100, "cache_read": 0}))
    run_step, check_quota = hooks(store, secrets, box, openrouter())
    assert check_quota(["qwen/qwen3-coder"])[0].ok
    run_step(engine_for(openrouter(), secrets), StepSpec("judge", "qwen/qwen3-coder", "Judge."), tmp_path)
    assert box.volumes == [None]
    assert store.spent("openrouter", 0) == 0.25
    claude = Box(StepResult(True, {}, cost_usd=1.5))
    hooks(store, secrets, claude, Account("claude", "claude-code"))[0](
        None, StepSpec("judge", "opus", "J."), tmp_path
    )
    assert claude.volumes == ["lastcall-account-claude"]
    assert store.spent("claude", 0) == 0  # a subscription's cost is an equivalent, not money: no ledger


def test_a_step_with_tokens_and_no_price_stops_the_run(store: Store, secrets: SecretStore, tmp_path: Path) -> None:
    secrets.set("key-openrouter", "sk-or-test")
    unpriced = StepResult(True, {}, cost_usd=0.0, usage={"input": 900, "output": 100, "cache_read": 0})
    run_step, check_quota = hooks(store, secrets, Box(unpriced), openrouter())
    run_step(None, StepSpec("judge", "some/new-model", "Judge."), tmp_path)
    verdict, _ = check_quota(["some/new-model"])
    assert not verdict.ok and "no price" in verdict.reason
    free_step, free_check = hooks(store, secrets, Box(unpriced), openrouter())
    free_step(None, StepSpec("judge", "qwen/qwen3-coder:free", "Judge."), tmp_path)
    assert free_check(["qwen/qwen3-coder:free"])[0].ok  # a free model costs nothing, rightly


# the command line


def run_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LASTCALL_SECRETS", str(tmp_path / "secrets"))
    return cli.main(list(argv))


def test_the_command_line_adds_an_account_on_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key_file = tmp_path / "openrouter.key"
    key_file.write_text("sk-or-test\n", encoding="utf-8")
    add = ("account", "add", "openrouter", "--engine", "opencode", "--provider", "openrouter")
    with pytest.raises(SystemExit):  # no budget
        run_cli(monkeypatch, tmp_path, *add, "--model", "qwen/qwen3-coder", "--key-file", str(key_file))
    assert "monthly budget" in capsys.readouterr().err
    assert (
        run_cli(
            monkeypatch, tmp_path, *add, "--model", "qwen/qwen3-coder", "--key-file", str(key_file), "--budget", "20"
        )
        == 0
    )
    assert "It needs no login" in capsys.readouterr().out
    assert SecretStore(tmp_path / "secrets").get("key-openrouter") == "sk-or-test"
    row = Store(tmp_path / "state" / "lastcall.db").account("openrouter")
    assert row is not None and (row.policy, row.budget_usd, row.models["deep"]) == (
        "budget",
        20.0,
        "qwen/qwen3-coder",
    )
    nights = ("account", "add", "local", "--engine", "opencode", "--provider", "ollama", "--model", "qwen3-coder")
    assert run_cli(monkeypatch, tmp_path, *nights, "--policy", "schedule", "--schedule", "* 1-6 * * *") == 0
    assert run_cli(monkeypatch, tmp_path, "account", "list") == 0
    listed = capsys.readouterr().out
    assert "opencode/openrouter" in listed and "budget 20 USD a month" in listed and "schedule * 1-6 * * *" in listed
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, tmp_path, "login", "local", "--engine", "opencode")
    assert "has no login" in capsys.readouterr().err


def test_models_of_a_subscription(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert (
        run_cli(monkeypatch, tmp_path, "account", "add", "claude", "--engine", "claude-code", "--model", "sonnet")
        == 0
    )
    row = Store(tmp_path / "state" / "lastcall.db").account("claude")
    assert row is not None and row.account().tiers()["deep"] == ("sonnet", "xhigh")
    models: Mapping[str, str] = row.models
    assert set(models) == {"fast", "balanced", "deep"}


def test_a_project_names_its_project_in_the_error_tracker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert run_cli(monkeypatch, tmp_path, "project", "add", "acme/shop", "--signals", "shop") == 0
    project = Store(tmp_path / "state" / "lastcall.db").project("acme/shop")
    assert project is not None and project.signals == "shop"
