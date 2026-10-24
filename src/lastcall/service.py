"""The lastcall service: it watches every account's quota and, when an expiring window would reset with capacity
left over, spends that capacity on the active projects (DESIGN.md, sections 5 and 6).

Every minute it ticks:
- every 15 minutes it reads the quota of each enabled account (no model call), learns the burst rate from the
  account's readings and plans whether to burst;
- every 10 minutes it reads from GitHub what the owner did with lastcall's pull requests and issues;
- every 6 hours it refreshes the suggestions for the installed repositories;
- the owner's orders ("audit this project now", from the web UI) start on a free account;
- each bursting account that is free starts a full audit of the project the ranking puts first. One run per account
  at a time, and never the same project by two accounts at once. Inside a run, a full 5-hour window makes the run
  wait for its reset; the reserve or any paid usage stops it.

`lastcall pause` stops new bursts (a running one finishes, and the owner's orders still start); `lastcall resume`
lets them start again.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable
from typing import Any

from lastcall import i18n, projects
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
        audit: Callable[[str, AccountRow, str], Any] | None = None,
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
        self.alerts: set[tuple[str, str]] = set()  # (account, "quota" or "paid") already told to the owner
        self.last_read = self.last_sync = self.last_refresh = 0.0
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
        self.inst.store.set_setting("heartbeat", f"{now:.0f}")  # the web UI tells whether the service is alive
        if now - self.last_read >= READ_EVERY:
            self.read_quotas(now)
        if now - self.last_sync >= SYNC_EVERY:
            self.last_sync = now
            try:
                sync(self.inst.store, self.inst.client, self.log)
            except (GitHubError, OSError) as e:
                self.log(f"sync: {e}")
        if now - self.last_refresh >= projects.REFRESH_EVERY:
            self.last_refresh = now
            try:
                projects.refresh(self.inst.store, self.inst.client, now)
            except (GitHubError, OSError) as e:
                self.log(f"suggestions: {e}")
        self.take_orders(now)
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
                self.alert(account.id, "quota", True, error=str(e)[:400])
                continue
            self.alert(account.id, "quota", False)
            self.inst.store.record_reading(reading)
            policy = SpendPolicy(reserve_pct=account.reserve_pct)
            history = self.inst.store.readings(account.id, now - RATE_WINDOW)
            rate = learned_rate(history, policy, "week", "5h", DEFAULT_RATE)
            before = self.plans.get(account.id)
            self.plans[account.id] = current = plan(reading, policy, rate, now)
            if current.burst and not (before and before.burst):
                self.log(f"{account.id}: burst starts ({current.reason}; {rate:.1f}%/h)")
                if self.inst.notify:
                    lang = self.inst.notify.language()
                    self.inst.notify.notify(
                        "burst",
                        account=account.id,
                        leftover=i18n.percent(lang, max(0.0, current.leftover_pct)),
                        span=i18n.span(lang, (current.hours_to_reset or 0) * 3600),
                    )
            elif before and before.burst and not current.burst:
                self.log(f"{account.id}: burst over ({current.reason})")
            if reading.paid_usage:
                self.log(f"{account.id}: paid usage is active on this account: lastcall spends nothing from it")
            self.alert(account.id, "paid", reading.paid_usage)

    def alert(self, account: str, event: str, on: bool, **values: str) -> None:
        """Tell the owner when a problem starts, once; forget it when it ends, so the next one is told again."""
        if not on:
            self.alerts.discard((account, event))
        elif (account, event) not in self.alerts:
            self.alerts.add((account, event))
            if self.inst.notify:
                self.inst.notify.notify(event, account=account, **values)

    # the owner's orders

    def take_orders(self, now: float) -> None:
        """Start the owner's orders on a free account: the one the order names, or the one with the most quota left
        before its reset. An order waits while its project is running or no account is free."""
        store = self.inst.store
        accounts = store.accounts(enabled_only=True)
        for order in store.orders():
            with self.lock:
                busy, free = set(self.running.values()), [a for a in accounts if a.id not in self.running]
            if order["repo"] in busy:
                continue
            if order["account"]:
                free = [a for a in free if a.id == order["account"]]
            if not free:
                continue
            account = max(free, key=lambda a: self.plans[a.id].leftover_pct if a.id in self.plans else -1.0)
            store.start_order(order["id"], now)
            self.log(f"{account.id}: audit of {order['repo']} ordered by the owner")
            self.launch(account, order["repo"], "manual")

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

    def launch(self, account: AccountRow, repo: str, trigger: str = "burst") -> None:
        with self.lock:
            self.running[account.id] = repo

        def work() -> None:
            try:
                self.audit(repo, account, trigger)
            except Exception:
                self.log(f"{account.id} on {repo}: {traceback.format_exc()}")
            finally:
                with self.lock:
                    self.running.pop(account.id, None)

        self.start(work)

    def _audit(self, repo: str, account: AccountRow, trigger: str) -> Any:
        project = self.inst.store.project(repo)
        opts = Options(
            language=(project.language if project and project.language else None) or account.language,
            max_prs=project.max_prs if project else 3,
            policy=SpendPolicy(reserve_pct=account.reserve_pct),
            wait=True,
            trigger=trigger,
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
