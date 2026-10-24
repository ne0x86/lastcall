from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall.accounts import Account
from lastcall.github import GitHub
from lastcall.quota import HOUR, QuotaError, Reading, Window
from lastcall.runs import Instance
from lastcall.sandbox import Sandbox
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
) -> Service:
    def read(account: Account, now: float) -> Reading:
        value = readings[account.id]
        if isinstance(value, Exception):
            raise value
        return value

    def audit(repo: str, account: AccountRow) -> Any:
        audits.append((account.id, repo))

    inst = Instance(cast(Sandbox, None), cast(GitHub, NoGitHub()), store)
    return Service(inst, read=read, audit=audit, start=started.append, clock=clock, log=lambda m: None)


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
