"""The lastcall service: it watches every account's quota and, when an expiring window would reset with capacity
left over, spends that capacity on the active projects (DESIGN.md, sections 5 and 6).

Every minute it ticks:
- every 15 minutes it reads the quota of each enabled account (no model call), learns the burst rate from the
  account's readings and plans whether to burst;
- every 10 minutes it reads from GitHub what the owner did with lastcall's pull requests and issues;
- each bursting account that is free starts a full audit of the project the ranking puts first. One run per account
  at a time, and never the same project by two accounts at once. Inside a run, a full 5-hour window makes the run
  wait for its reset; the reserve or any paid usage stops it.

`lastcall pause` stops new runs (a running one finishes); `lastcall resume` lets them start again.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from lastcall.accounts import Account, read_quota
from lastcall.github import GitHubError
from lastcall.quota import Plan, QuotaError, Reading, SpendPolicy, learned_rate, plan
from lastcall.ranking import rank
from lastcall.runs import Instance, Options, audit_repo
from lastcall.sandbox import SandboxError
from lastcall.store import AccountRow
from lastcall.sync import sync

READ_EVERY = 15 * 60.0
SYNC_EVERY = 10 * 60.0
TICK = 60.0
RATE_WINDOW = 14 * 86_400.0  # readings used to learn the burst rate
DEFAULT_RATE = 2.0  # % of the week per hour until there is enough to learn from
Log = Callable[[str], None]


class Service:
    def __init__(
        self,
        inst: Instance,
        *,
        read: Callable[[Account, float], Reading] | None = None,
        audit: Callable[[str, AccountRow], Any] | None = None,
        start: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.time,
        log: Log = print,
    ) -> None:
        self.inst, self.clock, self.log = inst, clock, log
        self.read = read or (lambda account, now: read_quota(account, inst.box, now))
        self.audit = audit or self._audit
        self.start = start or (lambda work: threading.Thread(target=work, daemon=True).start())
        self.running: dict[str, str] = {}  # account id -> repository
        self.plans: dict[str, Plan] = {}
        self.last_read = self.last_sync = 0.0
        self.lock = threading.Lock()

    # the loop

    def serve(self) -> None:
        stale = self.inst.store.interrupt_running()
        if stale:
            self.log(f"{stale} run(s) were interrupted when lastcall last stopped")
        self.log("lastcall is serving: quota every 15 min, pull requests every 10 min")
        while True:
            try:
                self.tick()
            except Exception:
                self.log(traceback.format_exc())
            time.sleep(TICK)

    def tick(self) -> None:
        now = self.clock()
        if now - self.last_read >= READ_EVERY:
            self.read_quotas(now)
        if now - self.last_sync >= SYNC_EVERY:
            self.last_sync = now
            try:
                sync(self.inst.store, self.inst.client, self.log)
            except (GitHubError, OSError) as e:
                self.log(f"sync: {e}")
        if self.inst.store.setting("paused") == "yes":
            return
        for account in self.inst.store.accounts(enabled_only=True):
            p = self.plans.get(account.id)
            if p is None or not p.burst or account.id in self.running:
                continue
            repo = self.pick(account, now)
            if repo:
                self.launch(account, repo)

    # quota

    def read_quotas(self, now: float) -> None:
        self.last_read = now
        for account in self.inst.store.accounts(enabled_only=True):
            try:
                reading = self.read(Account(account.id, account.engine), now)
            except (QuotaError, SandboxError) as e:
                self.log(f"{account.id}: no quota reading: {e}")
                self.plans.pop(account.id, None)
                continue
            self.inst.store.record_reading(reading)
            policy = SpendPolicy(reserve_pct=account.reserve_pct)
            history = self.inst.store.readings(account.id, now - RATE_WINDOW)
            rate = learned_rate(history, policy, "week", "5h", DEFAULT_RATE)
            before = self.plans.get(account.id)
            self.plans[account.id] = current = plan(reading, policy, rate, now)
            if current.burst and not (before and before.burst):
                self.log(f"{account.id}: burst starts ({current.reason}; {rate:.1f}%/h)")
            elif before and before.burst and not current.burst:
                self.log(f"{account.id}: burst over ({current.reason})")
            if reading.paid_usage:
                self.log(f"{account.id}: paid usage is active on this account: lastcall spends nothing from it")

    # projects

    def pick(self, account: AccountRow, now: float) -> str | None:
        store = self.inst.store
        with self.lock:
            busy = set(self.running.values())
        ranked = rank(
            store.projects(active_only=True),
            last_run=lambda repo: store.last_run(repo, account.id),
            open_prs=lambda repo: len(store.open_prs(repo)),
            pending=store.pending_count,
            new_commits=self.new_commits,
            busy=busy,
            now=now,
        )
        if not ranked:
            return None
        top = ranked[0]
        self.log(f"{account.id}: next is {top.repo} ({top.score:g} points: {'; '.join(top.reasons)})")
        return top.repo

    def new_commits(self, repo: str, sha: str) -> int | None:
        client = self.inst.client
        try:
            token = client.repository_token(repo)
            branch = client.repository(token, repo)["default_branch"]
            return int(client.request("GET", f"/repos/{repo}/compare/{sha}...{branch}", token).get("ahead_by", 0))
        except (GitHubError, OSError, ValueError):
            return None

    def launch(self, account: AccountRow, repo: str) -> None:
        with self.lock:
            self.running[account.id] = repo

        def work() -> None:
            try:
                self.audit(repo, account)
            except Exception:
                self.log(f"{account.id} on {repo}: {traceback.format_exc()}")
            finally:
                with self.lock:
                    self.running.pop(account.id, None)

        self.start(work)

    def _audit(self, repo: str, account: AccountRow) -> Any:
        project = self.inst.store.project(repo)
        opts = Options(
            language=(project.language if project and project.language else None) or account.language,
            max_prs=project.max_prs if project else 3,
            policy=SpendPolicy(reserve_pct=account.reserve_pct),
            wait=True,
            trigger="burst",
        )
        prefix = f"[{account.id} {repo}]"
        return audit_repo(
            self.inst,
            repo,
            Account(account.id, account.engine),
            opts,
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
        )
