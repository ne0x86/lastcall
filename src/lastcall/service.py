"""The lastcall service: it watches every account's quota and, when an expiring window would reset with capacity
left over, spends that capacity on the active projects (DESIGN.md, sections 5 and 6).

Every minute it ticks:
- every 15 minutes it reads the quota of each enabled account (no model call), learns the burst rate from the
  account's readings and plans whether to burst; an account on an API key or a local model plans every minute, from
  lastcall's own ledger, by its policy: its monthly budget at the pace of the month, or its schedule;
- every 10 minutes it reads from GitHub what the owner did with lastcall's pull requests and issues;
- every 6 hours it refreshes the suggestions for the installed repositories;
- every 30 minutes it counts each project's errors in production in the last day, from the owner's error tracker
  and from the logs of the project's containers and of its sources in Loki: projects with errors rank higher;
- the owner's orders ("audit this project now" or "improve it now", from the web UI) start on a free account; an
  audit can start on all of them together (fan-out);
- with fan-out on, accounts that burst at the same time review one project together, with one judge;
- each bursting account that is free starts a full audit of the project the ranking puts first; what the owner
  asked for goes first: the ideas ticked in an improvement plan, then the issues labelled `lastcall`. A project whose
  owner wants tests or dependency upgrades too gets them in turn with its audits: an audit, tests, upgrades, an
  audit... One run per account at a time, and never the same project by two accounts at once. Inside a run, a full
  5-hour window makes the run wait for its reset; the reserve or any paid usage stops it. An account does not burst
  while the window of a model that the audit needs, such as Claude's weekly window for Opus, is at the reserve.

`lastcall pause` stops new bursts (a running one finishes, and the owner's orders still start); `lastcall resume`
lets them start again.
"""

from __future__ import annotations

import threading
import time
import traceback
from collections.abc import Callable, Sequence
from typing import Any

from lastcall import i18n, logs, loki, projects, signals
from lastcall.accounts import Account, read_quota
from lastcall.github import GitHubError
from lastcall.quota import Plan, QuotaError, Reading, account_rate, plan
from lastcall.ranking import rank
from lastcall.runs import (
    Instance,
    Options,
    audit_models,
    audit_repo,
    deps_repo,
    improve_repo,
    issues_repo,
    tests_repo,
)
from lastcall.sandbox import SandboxError
from lastcall.store import AccountRow
from lastcall.sync import sync

READ_EVERY = 15 * 60.0
SIGNALS_EVERY = 30 * 60.0
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
        audit: Callable[[str, AccountRow, str, list[AccountRow]], Any] | None = None,
        improve: Callable[[str, AccountRow, str], Any] | None = None,
        issues: Callable[[str, AccountRow, str], Any] | None = None,
        tests: Callable[[str, AccountRow, str], Any] | None = None,
        deps: Callable[[str, AccountRow, str], Any] | None = None,
        start: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.time,
        log: Log = print,
    ) -> None:
        self.inst, self.clock, self.log = inst, clock, log
        self.read = read or (lambda account, now: read_quota(account, inst.box, now, inst.store, inst.secrets))
        self.audit = audit or self._audit
        self.improve = improve or self._improve
        self.issues = issues or self._issues
        self.tests = tests or self._tests
        self.deps = deps or self._deps
        self.start = start or (lambda work: threading.Thread(target=work, daemon=True).start())
        self.running: dict[str, str] = {}  # account id -> repository
        self.plans: dict[str, Plan] = {}
        self.alerts: set[tuple[str, str]] = set()  # (account, "quota" or "paid") already told to the owner
        self.last_read = self.last_sync = self.last_refresh = self.last_signals = 0.0
        self.errors: dict[str, int] = {}  # repository -> errors in production in the last day
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
        self.read_quotas(now, probe=now - self.last_read >= READ_EVERY)
        if now - self.last_sync >= SYNC_EVERY:
            self.last_sync = now
            try:
                sync(self.inst.store, self.inst.client, self.log, self.inst.notify)
            except (GitHubError, OSError) as e:
                self.log(f"sync: {e}")
        if now - self.last_signals >= SIGNALS_EVERY:
            self.last_signals = now
            self.read_signals(now)
        if now - self.last_refresh >= projects.REFRESH_EVERY:
            self.last_refresh = now
            try:
                projects.refresh(self.inst.store, self.inst.client, now)
            except (GitHubError, OSError) as e:
                self.log(f"suggestions: {e}")
        self.take_orders(now)
        if self.inst.store.setting("paused") == "yes":
            return
        if self.inst.store.setting("fanout") == "yes":
            self.fan_out(now)
        for account in self.inst.store.accounts(enabled_only=True):
            p = self.plans.get(account.id)
            if p is None or not p.burst or account.id in self.running:
                continue
            repo = self.pick(account, now)
            if repo:
                self.launch(account, repo, recipe=self.recipe(repo))

    # quota

    def read_quotas(self, now: float, probe: bool = True) -> None:
        """Read the quotas and plan. `probe`: also the accounts whose reading runs in the sandbox, and keep every
        reading; otherwise only the accounts on API keys and local models, whose reading is lastcall's ledger."""
        if probe:
            self.last_read = now
        for account in self.inst.store.accounts(enabled_only=True):
            if probe or account.keyed:
                self.read_account(account, now, record=probe)

    def read_account(self, account: AccountRow, now: float, record: bool = True) -> None:
        try:
            acc = account.account()
            reading = self.read(acc, now)
        except (QuotaError, SandboxError, ValueError) as e:
            if record or account.id in self.plans:  # a failing ledger is told once, not every minute
                self.log(f"{account.id}: no quota reading: {e}")
            self.plans.pop(account.id, None)
            self.alert(account.id, "quota", True, error=str(e)[:400])
            return
        self.alert(account.id, "quota", False)
        if record:
            self.inst.store.record_reading(reading)
        policy = account.spend_policy()
        history = self.inst.store.readings(account.id, now - RATE_WINDOW) if not account.keyed else []
        rate = account_rate(history, reading, policy, DEFAULT_RATE)
        before = self.plans.get(account.id)
        self.plans[account.id] = current = plan(reading, policy, rate, now, audit_models(acc))
        if current.burst and not (before and before.burst):
            pace = "" if account.keyed else f"; {rate:.1f}%/h"
            self.log(f"{account.id}: burst starts ({current.reason}{pace})")
            self.tell_burst(account, current)
        elif before and before.burst and not current.burst:
            self.log(f"{account.id}: burst over ({current.reason})")
        if reading.paid_usage and record:
            self.log(f"{account.id}: paid usage is active on this account: lastcall spends nothing from it")
        self.alert(account.id, "paid", reading.paid_usage)

    def tell_burst(self, account: AccountRow, current: Plan) -> None:
        """Tell the owner that a burst starts, in the terms of the account's policy."""
        notify = self.inst.notify
        if notify is None:
            return
        lang = notify.language()
        if current.code == "schedule":
            notify.notify("burst", "burst-schedule", account=account.id, cron=account.schedule)
        elif current.budget is not None and account.budget_usd:
            spent = i18n.money(lang, current.budget.used_pct * account.budget_usd / 100)
            budget = i18n.money(lang, account.budget_usd)
            notify.notify("burst", "burst-budget", account=account.id, spent=spent, budget=budget)
        else:
            notify.notify(
                "burst",
                account=account.id,
                leftover=i18n.percent(lang, max(0.0, current.leftover_pct)),
                span=i18n.span(lang, (current.hours_to_reset or 0) * 3600),
            )

    def read_signals(self, now: float) -> None:
        """How many errors each active project had in production in the last day: from the owner's error tracker,
        and kinds of errors in the logs of its containers and of its sources in Loki."""
        tracker = signals.Tracker.load(self.inst.secrets) if self.inst.secrets is not None else None
        connection = loki.Loki.load(self.inst.secrets) if self.inst.secrets is not None else None
        sources = loki.chosen(self.inst.store)
        choices, listed = logs.chosen(self.inst.store), list[dict[str, Any]]()
        try:
            listed = self.inst.box.containers() if self.inst.box is not None else []
        except SandboxError as e:
            self.log(f"the containers were not listed: {e}")
        counts: dict[str, int] = {}
        for p in self.inst.store.projects(active_only=True):
            if tracker is not None and p.signals:
                try:
                    found = signals.errors(tracker, p.signals, (), now, self.inst.fetch, 1, 100, frames=False)
                    counts[p.repo] = counts.get(p.repo, 0) + len(found)
                except signals.SignalsError as e:
                    self.log(f"{p.repo}: the errors in production were not read: {e}")
            names = logs.names_for(p.repo, listed, choices)
            if names:
                try:
                    kinds = logs.errors(self.inst.box.logs(names, logs.HOURS * 3600))[1]
                    counts[p.repo] = counts.get(p.repo, 0) + len(kinds)
                except SandboxError as e:
                    self.log(f"{p.repo}: the container logs were not read: {e}")
            if connection is not None and loki.names_for(p.repo, sources):
                try:
                    kinds = loki.read(connection, self.inst.store, p.repo, (), now, self.inst.loki_fetch)[1]
                    counts[p.repo] = counts.get(p.repo, 0) + len(kinds)
                except loki.LokiError as e:
                    self.log(f"{p.repo}: the logs in Loki were not read: {e}")
        self.errors = {repo: n for repo, n in counts.items() if n}

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
            if order["account"] and order["account"] != "*":
                free = [a for a in free if a.id == order["account"]]
            if not free:
                continue
            account = max(free, key=self.leftover)
            recipe = order["kind"] if order["kind"] in ("improve", "issues", "tests", "deps") else "audit"
            crew = [a for a in free if a is not account] if order["account"] == "*" and recipe == "audit" else []
            store.start_order(order["id"], now)
            self.log(f"{account.id}: {recipe} of {order['repo']} ordered by the owner")
            self.launch(account, order["repo"], "manual", crew, recipe)

    def leftover(self, account: AccountRow) -> float:
        """The quota an account has left before its reset; -1 when it has no plan yet."""
        return self.plans[account.id].leftover_pct if account.id in self.plans else -1.0

    def fan_out(self, now: float) -> None:
        """Accounts that burst at the same time review one project together: the one with the most left judges
        and implements."""
        with self.lock:
            bursting = [
                a
                for a in self.inst.store.accounts(enabled_only=True)
                if a.id not in self.running and a.id in self.plans and self.plans[a.id].burst
            ]
        if len(bursting) < 2:
            return
        lead = max(bursting, key=self.leftover)
        repo = self.pick(lead, now)
        if repo:
            recipe = self.recipe(repo)
            crew = [a for a in bursting if a is not lead] if recipe == "audit" else []
            self.launch(lead, repo, "burst", crew, recipe)

    def recipe(self, repo: str) -> str:
        """What a burst does with a project: what the owner asked for first (the ideas ticked in a plan, then the
        labelled issues), or else the next in the turn of the recipes the owner wants for the project: an audit,
        then tests, then upgrades, then an audit again."""
        store = self.inst.store
        if store.wanted_count(repo):
            return "improve"
        if store.waiting_count(repo):
            return "issues"
        project, last = store.project(repo), store.latest_runs().get(repo)
        turn = ["audit", *(r for r in ("tests", "deps") if project and getattr(project, r))]
        previous = str((last or {}).get("recipe") or "")
        return turn[(turn.index(previous) + 1) % len(turn)] if previous in turn else "audit"

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
            wanted=lambda repo: store.wanted_count(repo) + store.waiting_count(repo),
            errors=lambda repo: self.errors.get(repo, 0),
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

    def launch(
        self,
        account: AccountRow,
        repo: str,
        trigger: str = "burst",
        crew: Sequence[AccountRow] = (),
        recipe: str = "audit",
    ) -> None:
        """Start a run on `account`, with `crew` reviewing too. They all stay busy until it ends."""
        members = [account, *crew]
        with self.lock:
            for a in members:
                self.running[a.id] = repo

        def work() -> None:
            try:
                if recipe == "improve":
                    self.improve(repo, account, trigger)
                elif recipe == "issues":
                    self.issues(repo, account, trigger)
                elif recipe == "tests":
                    self.tests(repo, account, trigger)
                elif recipe == "deps":
                    self.deps(repo, account, trigger)
                else:
                    self.audit(repo, account, trigger, list(crew))
            except Exception:
                self.log(f"{account.id} on {repo}: {traceback.format_exc()}")
            finally:
                with self.lock:
                    for a in members:
                        self.running.pop(a.id, None)

        self.start(work)

    def options(self, repo: str, account: AccountRow, trigger: str) -> Options:
        project = self.inst.store.project(repo)
        return Options(
            language=(project.language if project and project.language else None) or account.language,
            max_prs=project.max_prs if project else 3,
            policy=account.spend_policy(),
            wait=True,
            trigger=trigger,
        )

    def _audit(self, repo: str, account: AccountRow, trigger: str, crew: list[AccountRow]) -> Any:
        prefix = f"[{account.id} {repo}]"
        return audit_repo(
            self.inst,
            repo,
            account.account(),
            self.options(repo, account, trigger),
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
            peers=[(c.account(), c.spend_policy()) for c in crew],
        )

    def _issues(self, repo: str, account: AccountRow, trigger: str) -> Any:
        prefix = f"[{account.id} {repo}]"
        return issues_repo(
            self.inst,
            repo,
            account.account(),
            self.options(repo, account, trigger),
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
        )

    def _tests(self, repo: str, account: AccountRow, trigger: str) -> Any:
        prefix = f"[{account.id} {repo}]"
        return tests_repo(
            self.inst,
            repo,
            account.account(),
            self.options(repo, account, trigger),
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
        )

    def _deps(self, repo: str, account: AccountRow, trigger: str) -> Any:
        prefix = f"[{account.id} {repo}]"
        return deps_repo(
            self.inst,
            repo,
            account.account(),
            self.options(repo, account, trigger),
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
        )

    def _improve(self, repo: str, account: AccountRow, trigger: str) -> Any:
        prefix = f"[{account.id} {repo}]"
        return improve_repo(
            self.inst,
            repo,
            account.account(),
            self.options(repo, account, trigger),
            on_step=lambda s: self.log(f"{prefix} {s.role}: {'ok' if s.ok else 'failed'} in {s.seconds:g} s"),
            log=lambda message: self.log(f"{prefix} {message}"),
        )
