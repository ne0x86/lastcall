from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall.accounts import Account
from lastcall.github import GitHub
from lastcall.notify import Notifier
from lastcall.quota import HOUR, QuotaError, Reading, Window
from lastcall.recipes.improve import ImproveReport
from lastcall.runs import Instance
from lastcall.sandbox import Sandbox
from lastcall.secretstore import SecretStore
from lastcall.service import READ_EVERY, SYNC_EVERY, Service
from lastcall.store import AccountRow, Store

NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def reading(account: str, week: float, reset_h: float, five: float = 10.0) -> Reading:
    return Reading(
        account,
        (
            Window("5h", five, NOW + 2 * HOUR, "throttle", 5.0),
            Window("week", week, NOW + reset_h * HOUR, "expiring", 168.0),
        ),
        False,
        "exact",
        NOW,
    )


class NoGitHub:
    """sync finds nothing to read, and the ranking finds no new commits."""

    def repository_token(self, repo: str) -> str:
        raise OSError("offline")

    def installations(self) -> list[dict[str, Any]]:
        raise OSError("offline")


@pytest.fixture
def store(tmp_path: Path) -> Store:
    st = Store(tmp_path / "lastcall.db")
    st.add_account("claude", "claude-code")
    st.add_account("chatgpt", "codex")
    st.add_project("ne0x86/a")
    st.add_project("ne0x86/b")
    return st


def service(
    store: Store,
    readings: dict[str, Reading | Exception],
    audits: list[tuple[str, str]],
    clock: Clock,
    started: list[Callable[[], None]],
    log: Callable[[str], None] = lambda m: None,
) -> Service:
    def read(account: Account, now: float) -> Reading:
        value = readings[account.id]
        if isinstance(value, Exception):
            raise value
        return value

    def audit(repo: str, account: AccountRow, trigger: str, crew: list[AccountRow]) -> Any:
        who = "+".join([account.id, *(c.id for c in crew)])
        audits.append((who, repo) if trigger == "burst" else (who, f"{repo} ({trigger})"))

    inst = Instance(cast(Sandbox, None), cast(GitHub, NoGitHub()), store)
    return Service(inst, read=read, audit=audit, start=started.append, clock=clock, log=log)


def test_bursting_accounts_audit_different_projects(store: Store) -> None:
    audits: list[tuple[str, str]] = []
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 30, 20),
        "chatgpt": reading("chatgpt", 30, 20),
    }
    svc = service(store, readings, audits, Clock(), started)
    svc.tick()
    assert sorted(svc.running.values()) == ["ne0x86/a", "ne0x86/b"]  # never the same project twice at once
    svc.tick()
    assert len(started) == 2  # one run per account at a time
    for work in started:
        work()
    assert sorted(audits) == [("chatgpt", "ne0x86/a"), ("claude", "ne0x86/b")]
    assert svc.running == {}
    assert len(store.readings("claude", 0)) == 1


def test_no_burst_far_from_the_reset(store: Store) -> None:
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 150), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], Clock(), started)
    svc.tick()
    assert started == []
    assert not svc.plans["claude"].burst
    assert "chatgpt" not in svc.plans  # no reading: nothing is spent


def test_pause_stops_new_runs(store: Store) -> None:
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 20), "chatgpt": QuotaError("x")}
    store.set_setting("paused", "yes")
    svc = service(store, readings, [], Clock(), started)
    svc.tick()
    assert started == []
    store.set_setting("paused", "no")
    svc.tick()
    assert len(started) == 1


def test_no_new_burst_after_the_reset_its_plan_was_made_for(store: Store) -> None:
    # the plan is made at a reading 10 min before the reset; the next reading is 15 min later
    clock, started = Clock(), list[Callable[[], None]]()
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 68, 10 / 60), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], clock, started)
    svc.tick()
    assert len(started) == 1 and svc.plans["claude"].burst
    started.pop()()  # the burst run ends 5 min before the reset
    clock.now += 11 * 60  # a minute after the reset: the leftover it was for is gone
    svc.tick()
    assert started == [] and svc.running == {}
    clock.now += READ_EVERY  # the next reading plans again, from the new window
    readings["claude"] = reading("claude", 0, 168)
    svc.tick()
    assert started == [] and svc.plans["claude"].code == "later"


def test_accounts_whose_reset_passed_do_not_fan_out(store: Store) -> None:
    clock, started = Clock(), list[Callable[[], None]]()
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 68, 10 / 60),
        "chatgpt": reading("chatgpt", 68, 10 / 60),
    }
    svc = service(store, readings, [], clock, started)
    svc.read_quotas(clock.now)
    store.set_setting("fanout", "yes")
    clock.now += 11 * 60
    svc.tick()
    assert started == [] and svc.running == {}


def test_quota_and_sync_have_their_own_rhythm(store: Store) -> None:
    clock = Clock()
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 150), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], clock, [])
    svc.tick()
    clock.now += READ_EVERY - 1
    svc.tick()
    assert len(store.readings("claude", 0)) == 1
    clock.now += 1
    svc.tick()
    assert len(store.readings("claude", 0)) == 2
    assert svc.last_sync == NOW + READ_EVERY - 1  # 899 s after the first sync is over 10 min: synced again
    assert READ_EVERY - 1 >= SYNC_EVERY


def test_the_owners_orders_start_on_a_free_account_even_when_paused(store: Store) -> None:
    audits: list[tuple[str, str]] = []
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 30, 150),  # far from the reset: no burst
        "chatgpt": reading("chatgpt", 70, 150),
    }
    store.set_setting("paused", "yes")
    store.add_order("audit", "ne0x86/a", None, NOW)
    store.add_order("audit", "ne0x86/a", "chatgpt", NOW)
    svc = service(store, readings, audits, Clock(), started)
    svc.tick()
    assert svc.running == {"claude": "ne0x86/a"}  # the account with the most left before its reset
    assert len(store.orders()) == 1  # the same project waits for the first run to end
    started.pop()()
    svc.tick()
    assert svc.running == {"chatgpt": "ne0x86/a"}
    started.pop()()
    assert audits == [("claude", "ne0x86/a (manual)"), ("chatgpt", "ne0x86/a (manual)")]
    assert store.orders() == []


def test_problems_and_bursts_are_told_once(store: Store, tmp_path: Path) -> None:
    told: list[str] = []

    def send(urls: Sequence[str], title: str, body: str) -> tuple[bool, str]:
        told.append(title)
        return True, ""

    notifier = Notifier(SecretStore(tmp_path / "secrets"), store, send=send, log=lambda m: None)
    notifier.add_url("ntfy://ntfy.sh/lastcall-topic")
    notifier.choose(["quota", "paid", "burst"], "en")
    paid = Reading("chatgpt", reading("chatgpt", 30, 150).windows, True, "exact", NOW)
    readings: dict[str, Reading | Exception] = {"claude": QuotaError("not signed in"), "chatgpt": paid}

    def read(account: Account, now: float) -> Reading:
        value = readings[account.id]
        if isinstance(value, Exception):
            raise value
        return value

    inst = Instance(cast(Sandbox, None), cast(GitHub, NoGitHub()), store, notifier)
    svc = Service(inst, read=read, audit=lambda *a: None, start=lambda w: None, clock=Clock(), log=lambda m: None)
    svc.read_quotas(NOW)
    svc.read_quotas(NOW + READ_EVERY)
    assert told == ["lastcall: paid usage on chatgpt", "lastcall: cannot read the quota of claude"]
    readings["claude"] = reading("claude", 30, 20)  # signed in again, and the reset is close
    svc.read_quotas(NOW + 2 * READ_EVERY)
    assert told[-1] == "lastcall: claude starts a burst"
    readings["claude"] = QuotaError("expired")
    svc.read_quotas(NOW + 3 * READ_EVERY)
    assert told[-1] == "lastcall: cannot read the quota of claude"  # a new problem is told again
    assert len(told) == 4


def test_an_order_for_all_accounts_fans_out(store: Store) -> None:
    audits: list[tuple[str, str]] = []
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 30, 150),
        "chatgpt": reading("chatgpt", 70, 150),
    }
    store.add_order("audit", "ne0x86/a", "*", NOW)
    svc = service(store, readings, audits, Clock(), started)
    svc.tick()
    assert svc.running == {"claude": "ne0x86/a", "chatgpt": "ne0x86/a"}  # both busy until the run ends
    started.pop()()
    assert audits == [("claude+chatgpt", "ne0x86/a (manual)")]  # the one with the most left leads
    assert svc.running == {}


def test_accounts_that_burst_together_review_together_when_the_owner_wants(store: Store) -> None:
    audits: list[tuple[str, str]] = []
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 30, 20),
        "chatgpt": reading("chatgpt", 50, 20),
    }
    store.set_setting("fanout", "yes")
    svc = service(store, readings, audits, Clock(), started)
    svc.tick()
    assert len(started) == 1 and set(svc.running) == {"claude", "chatgpt"}
    started.pop()()
    (who, repo) = audits[0]
    assert who == "claude+chatgpt" and repo in ("ne0x86/a", "ne0x86/b")


def test_no_burst_while_the_judges_model_is_out_of_room(store: Store) -> None:
    """Claude's own window for Opus, the model of the deep tier, is at the reserve: a burst could not judge."""
    base = reading("claude", 30, 20)
    opus = Window("week", 95, NOW + 40 * HOUR, "expiring", 168.0, "Opus")
    readings: dict[str, Reading | Exception] = {
        "claude": Reading("claude", (*base.windows, opus), False, "exact", NOW),
        "chatgpt": QuotaError("x"),
    }
    started: list[Callable[[], None]] = []
    svc = service(store, readings, [], Clock(), started)
    svc.tick()
    assert started == []
    assert svc.plans["claude"].code == "model"


def test_an_improvement_order_runs_the_improve_recipe(store: Store) -> None:
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 30, 150),
        "chatgpt": reading("chatgpt", 30, 150),
    }
    audits: list[tuple[str, str]] = []
    svc = service(store, readings, audits, Clock(), started)
    improved: list[tuple[str, str, str]] = []
    svc.improve = lambda repo, account, trigger: improved.append((account.id, repo, trigger))
    store.add_order("improve", "ne0x86/a", "*", NOW)  # "all accounts" means any free one: nobody else reviews
    svc.tick()
    assert len(svc.running) == 1
    for work in started:
        work()
    assert [(repo, trigger) for _, repo, trigger in improved] == [("ne0x86/a", "manual")] and audits == []


def test_a_burst_builds_the_ideas_the_owner_ticked(store: Store) -> None:
    plan = ImproveReport("ne0x86/b", "abc", "claude-code", "English")
    plan.ideas = [{"id": "I1", "title": "A", "kind": "feature", "change": "c"}]
    plan.verdicts, plan.plan = [{"id": "I1", "verdict": "keep"}], ["I1"]
    plan.issue_url = "https://github.com/ne0x86/b/issues/3"
    store.record_ideas("r0", plan)
    store.tick_ideas(plan.issue_url, {"I1": True})
    started: list[Callable[[], None]] = []
    audits: list[tuple[str, str]] = []
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 20), "chatgpt": QuotaError("x")}
    svc = service(store, readings, audits, Clock(), started)
    improved: list[tuple[str, str]] = []
    svc.improve = lambda repo, account, trigger: improved.append((account.id, repo))
    svc.tick()
    for work in started:
        work()
    assert improved == [("claude", "ne0x86/b")] and audits == []


def test_a_burst_works_on_labelled_issues_after_ticked_ideas(store: Store) -> None:
    store.sync_tickets("ne0x86/b", [{"number": 7, "url": "https://github.com/ne0x86/b/issues/7", "title": "Crash"}])
    started: list[Callable[[], None]] = []
    audits: list[tuple[str, str]] = []
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 20), "chatgpt": QuotaError("x")}
    svc = service(store, readings, audits, Clock(), started)
    done: list[tuple[str, str]] = []
    svc.issues = lambda repo, account, trigger: done.append(("issues", repo))
    svc.improve = lambda repo, account, trigger: done.append(("improve", repo))
    assert svc.recipe("ne0x86/b") == "issues" and svc.recipe("ne0x86/a") == "audit"
    svc.tick()
    for work in started:
        work()
    assert done == [("issues", "ne0x86/b")] and audits == []


# accounts on API keys and local models


def keyed_service(tmp_path: Path, clock: Clock, started: list[Callable[[], None]], told: list[str]) -> Service:
    """A service on the real reader: an account on a key or a local model reads the ledger, with no sandbox."""
    store = Store(tmp_path / "keyed.db")
    store.add_project("ne0x86/a")
    secrets = SecretStore(tmp_path / "secrets")

    def send(urls: Sequence[str], title: str, body: str) -> tuple[bool, str]:
        told.append(body)
        return True, ""

    notifier = Notifier(secrets, store, send=send, log=lambda m: None)
    notifier.add_url("ntfy://ntfy.sh/lastcall-topic")
    notifier.choose(["quota", "burst"], "en")
    inst = Instance(cast(Sandbox, None), cast(GitHub, NoGitHub()), store, notifier, secrets)
    return Service(inst, audit=lambda *a: None, start=started.append, clock=clock, log=lambda m: None)


def test_a_local_model_bursts_at_the_hours_of_its_schedule(tmp_path: Path) -> None:
    clock, started, told = Clock(), list[Callable[[], None]](), list[str]()  # NOW is 08:00 UTC
    svc = keyed_service(tmp_path, clock, started, told)
    store = svc.inst.store
    models = {"fast": "qwen3-coder", "balanced": "qwen3-coder", "deep": "qwen3-coder"}
    store.add_account(
        "local", "opencode", provider="ollama", models=models, policy="schedule", schedule="* 1-6 * * *"
    )
    svc.tick()
    assert started == [] and svc.plans["local"].code == "window"
    clock.now += 17 * HOUR - 60  # 00:59 UTC
    svc.tick()
    assert started == []
    clock.now += 60  # 01:00 UTC: the schedule begins, a minute after the last reading of the quotas
    svc.tick()
    assert len(started) == 1 and svc.running == {"local": "ne0x86/a"}
    assert told == ["Its schedule (* 1-6 * * *, UTC) has begun."]
    assert len(store.readings("local", 0)) == 2  # it plans every minute, and keeps a reading every 15 minutes


def test_a_key_without_its_key_never_bursts_and_says_so_once(tmp_path: Path) -> None:
    clock, started, told = Clock(), list[Callable[[], None]](), list[str]()
    svc = keyed_service(tmp_path, clock, started, told)
    models = {"fast": "m", "balanced": "m", "deep": "m"}
    svc.inst.store.add_account(
        "openrouter", "opencode", provider="openrouter", models=models, policy="budget", budget_usd=20
    )
    for _ in range(3):
        svc.tick()
        clock.now += 60
    assert started == [] and "openrouter" not in svc.plans
    assert told == [
        "no API key: give the account one on the Accounts page\nIf its login has expired, sign the account in again."
    ]
    assert svc.inst.secrets is not None
    svc.inst.secrets.set("key-openrouter", "sk-or-test")
    svc.tick()
    assert svc.plans["openrouter"].code == "pace" and len(started) == 1
    assert told[-1] == "$0.00 of its $20.00 budget spent this month, behind the calendar."


def test_a_project_that_wants_tests_gets_them_every_other_run(store: Store) -> None:
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 20), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], Clock(), started)
    tested: list[tuple[str, str, str]] = []
    svc.tests = lambda repo, account, trigger: tested.append((account.id, repo, trigger))
    store.update_project("ne0x86/a", active=True, weight=1, language=None, max_prs=3, tests=True)
    assert svc.recipe("ne0x86/a") == "audit"  # nothing ran yet: an audit first
    store.start_run("r1", "ne0x86/a", "claude", "burst")
    store.finish_run("r1", "ok", "abc1234")
    store.x("UPDATE runs SET started = 1 WHERE id = 'r1'")
    assert svc.recipe("ne0x86/a") == "tests" and svc.recipe("ne0x86/b") == "audit"  # b does not want tests
    store.start_run("r2", "ne0x86/a", "claude", "burst", recipe="tests")
    store.x("UPDATE runs SET started = 2 WHERE id = 'r2'")
    assert svc.recipe("ne0x86/a") == "audit"
    store.add_order("tests", "ne0x86/b", "claude", NOW)
    svc.tick()
    for work in started:
        work()
    assert tested == [("claude", "ne0x86/b", "manual")]


def test_the_recipes_take_turns_on_a_project(store: Store) -> None:
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 30, 150), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], Clock(), [])

    def turns(n: int) -> list[str]:
        done = []
        for _ in range(n):
            recipe = svc.recipe("ne0x86/a")
            done.append(recipe)
            run = f"r{len(store.runs(100))}"
            store.start_run(run, "ne0x86/a", "claude", "burst", recipe=recipe)
            store.x("UPDATE runs SET started = ? WHERE id = ?", (float(len(store.runs(100))), run))
        return done

    store.update_project("ne0x86/a", active=True, weight=1, language=None, max_prs=3, tests=True, deps=True)
    assert turns(4) == ["audit", "tests", "deps", "audit"]
    store.update_project("ne0x86/a", active=True, weight=1, language=None, max_prs=3, deps=True)
    assert turns(3) == ["deps", "audit", "deps"]  # after an audit; the tests are off now


def test_a_reading_taken_now_with_a_passed_reset_is_logged_once_for_each_reset(store: Store) -> None:
    clock, said = Clock(), list[str]()
    readings: dict[str, Reading | Exception] = {"claude": reading("claude", 20, -1), "chatgpt": QuotaError("x")}
    svc = service(store, readings, [], clock, [], said.append)
    svc.tick()
    asked = [m for m in said if m.startswith("claude: a reading taken now")]
    assert svc.plans["claude"].code == "passed" and len(asked) == 1
    assert "reset at 2027-01-15 07:00 UTC" in asked[0]  # an hour before NOW
    clock.now += READ_EVERY
    svc.tick()
    assert len([m for m in said if m.startswith("claude: a reading taken now")]) == 1  # the same reset: not again
    readings["claude"] = reading("claude", 20, 100)
    clock.now += READ_EVERY
    svc.tick()
    assert svc.plans["claude"].code != "passed" and svc.passed == {}
    readings["claude"] = reading("claude", 20, -0.5)
    clock.now += READ_EVERY
    svc.tick()
    assert len([m for m in said if m.startswith("claude: a reading taken now")]) == 2  # another reset: told again


def test_an_order_for_any_free_account_ignores_the_leftover_of_a_passed_plan(store: Store) -> None:
    # claude's reading is from before its reset: 80% would be left, but that leftover was lost at the reset
    audits: list[tuple[str, str]] = []
    started: list[Callable[[], None]] = []
    readings: dict[str, Reading | Exception] = {
        "claude": reading("claude", 10, -1),
        "chatgpt": reading("chatgpt", 70, 150),
    }
    store.add_order("audit", "ne0x86/a", "*", NOW)
    svc = service(store, readings, audits, Clock(), started)
    svc.read_quotas(NOW)
    assert svc.plans["claude"].code == "passed" and svc.plans["claude"].leftover_pct == 0.0
    svc.take_orders(NOW)
    started.pop()()
    assert audits == [("chatgpt+claude", "ne0x86/a (manual)")]  # chatgpt, with 20% left, leads: claude has none
