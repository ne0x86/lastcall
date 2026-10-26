"""lastcall's state in SQLite: accounts, projects, quota readings, what the steps of accounts on API keys cost (the
ledger), runs, findings, ideas, labelled issues, pull requests, the suggestions for the installed repositories, the
owner's orders and the web UI's sessions.

Findings keep their story across runs, so the judge never proposes again what the owner rejected:
    pending    valid, meant for a pull request, not opened yet (it did not fit, or delivery failed)
    in_pr      in an open lastcall pull request
    merged     its pull request was merged
    rejected   its pull request was closed without merging
    reverted   its pull request was merged, broke something, and its revert was merged
    decide     valid, waiting for the owner's decision in the run's issue
    resolved   the owner closed that issue as done
    dismissed  the owner closed that issue as not planned
    discarded  the judge discarded it

Ideas, from improvement plans, keep theirs too:
    proposed   in an open plan, not ticked
    wanted     the owner ticked it: a run builds it
    in_pr      in an open lastcall pull request
    merged     its pull request was merged
    rejected   its pull request was closed without merging
    reverted   its pull request was merged, broke something, and its revert was merged
    dismissed  its plan was closed without it
    stuck      a run tried to build it and could not
    discarded  the critic left it out of the plan

Issues that the owner labels `lastcall` (tickets) too:
    waiting    labelled and open: a run works on it
    in_pr      in an open lastcall pull request, which closes the issue when it is merged
    merged     its pull request was merged
    rejected   its pull request was closed without merging; lastcall takes the label off
    reverted   its pull request was merged, broke something, and its revert was merged
    stuck      a run could not do it, and said why on the issue; lastcall takes the label off
    dropped    it lost its label, or was closed, before a run did it
A rejected or stuck issue waits again when the owner labels it again.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lastcall.accounts import Account
from lastcall.quota import Kind, Reading, SpendPolicy, Window
from lastcall.suggest import Suggestion

if TYPE_CHECKING:
    from lastcall.recipes.audit import AuditReport
    from lastcall.recipes.improve import ImproveReport
    from lastcall.recipes.issues import IssuesReport

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, engine TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    language TEXT NOT NULL DEFAULT 'English', reserve_pct REAL NOT NULL DEFAULT 10, created REAL NOT NULL,
    provider TEXT NOT NULL DEFAULT '', base_url TEXT NOT NULL DEFAULT '', models TEXT NOT NULL DEFAULT '{}',
    policy TEXT NOT NULL DEFAULT 'before-reset', budget_usd REAL, schedule TEXT NOT NULL DEFAULT '',
    efforts TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS projects (
    repo TEXT PRIMARY KEY, active INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 1, language TEXT,
    max_prs INTEGER NOT NULL DEFAULT 3, created REAL NOT NULL, merge_on_approve INTEGER NOT NULL DEFAULT 0,
    watch INTEGER NOT NULL DEFAULT 0, health_url TEXT, tests INTEGER NOT NULL DEFAULT 0,
    deps INTEGER NOT NULL DEFAULT 0, signals TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS readings (
    account TEXT NOT NULL, taken_at REAL NOT NULL, windows TEXT NOT NULL, paid INTEGER NOT NULL,
    fidelity TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS readings_by_account ON readings (account, taken_at);
CREATE TABLE IF NOT EXISTS spend (
    account TEXT NOT NULL, at REAL NOT NULL, usd REAL NOT NULL, run TEXT NOT NULL DEFAULT '', role TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS spend_by_account ON spend (account, at);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, repo TEXT NOT NULL, account TEXT NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL,
    sha TEXT, started REAL NOT NULL, ended REAL, crew TEXT NOT NULL DEFAULT '', recipe TEXT NOT NULL DEFAULT 'audit');
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run TEXT NOT NULL, fid TEXT NOT NULL, data TEXT NOT NULL,
    verdict TEXT, decision TEXT, severity TEXT, reason TEXT, status TEXT NOT NULL, pr_url TEXT, issue_url TEXT,
    created REAL NOT NULL, UNIQUE (run, fid));
CREATE TABLE IF NOT EXISTS ideas (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, run TEXT NOT NULL, iid TEXT NOT NULL, data TEXT NOT NULL,
    status TEXT NOT NULL, issue_url TEXT, pr_url TEXT, note TEXT, created REAL NOT NULL, UNIQUE (run, iid));
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL, pr_url TEXT, note TEXT, unlabeled INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
    UNIQUE (repo, number));
CREATE TABLE IF NOT EXISTS prs (
    url TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, run TEXT NOT NULL, title TEXT NOT NULL,
    state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, kind TEXT NOT NULL DEFAULT 'change',
    merge_sha TEXT, watch_until REAL, health_failures INTEGER NOT NULL DEFAULT 0, reverted TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, csrf TEXT NOT NULL, created REAL NOT NULL, seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS suggestions (repo TEXT PRIMARY KEY, data TEXT NOT NULL, refreshed REAL NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, repo TEXT NOT NULL, account TEXT, created REAL NOT NULL,
    started REAL);
"""
REMEMBERED = ("pending", "in_pr", "merged", "rejected", "reverted", "decide", "resolved", "dismissed")


@dataclass(frozen=True)
class AccountRow:
    id: str
    engine: str
    enabled: bool
    language: str
    reserve_pct: float
    provider: str = ""  # an API's provider or a local model server; "" for an account on a CLI login
    base_url: str = ""
    models: Mapping[str, str] = field(default_factory=dict)  # tier -> model, over the engine's defaults
    policy: Kind = "before-reset"
    budget_usd: float | None = None  # per calendar month (UTC), for an account on an API key
    schedule: str = ""  # policy schedule: when it bursts, as a cron expression in UTC
    efforts: Mapping[str, str] = field(default_factory=dict)  # tier -> effort, over the engine's defaults

    @property
    def keyed(self) -> bool:
        return bool(self.provider)

    def account(self) -> Account:
        return Account(self.id, self.engine, self.provider, self.base_url, self.models, self.budget_usd, self.efforts)

    def spend_policy(self) -> SpendPolicy:
        """How it spends. An account on an API key keeps no reserve: its monthly budget is the owner's limit."""
        return SpendPolicy(self.policy, 0.0 if self.keyed else self.reserve_pct, cron=self.schedule)


@dataclass(frozen=True)
class ProjectRow:
    repo: str
    active: bool
    weight: float
    language: str | None
    max_prs: int
    merge_on_approve: bool = False  # lastcall merges its pull requests that the owner approves
    watch: bool = False  # after a merge, lastcall watches the checks and the health address, and proposes a revert
    health_url: str | None = None
    tests: bool = False  # bursts also write tests for it, in turn with the audits
    deps: bool = False  # bursts also upgrade its dependencies, in turn with the audits
    signals: str = ""  # its project in the owner's error tracker (Sentry or GlitchTip), by slug


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()
        self.db.executescript(SCHEMA)
        # crew came in v0.3; recipe, the projects' merge and watch settings and the pull requests' watch in v0.4;
        # accounts on API keys and local models, with their models, policies, budgets and schedules, and the
        # projects' tests, deps and signals settings, in v0.5
        self._add_columns(
            "accounts",
            {
                "provider": "TEXT NOT NULL DEFAULT ''",
                "base_url": "TEXT NOT NULL DEFAULT ''",
                "models": "TEXT NOT NULL DEFAULT '{}'",
                "policy": "TEXT NOT NULL DEFAULT 'before-reset'",
                "budget_usd": "REAL",
                "schedule": "TEXT NOT NULL DEFAULT ''",
                "efforts": "TEXT NOT NULL DEFAULT '{}'",  # v0.6.2
            },
        )
        self._add_columns("runs", {"crew": "TEXT NOT NULL DEFAULT ''", "recipe": "TEXT NOT NULL DEFAULT 'audit'"})
        self._add_columns(
            "projects",
            {
                "merge_on_approve": "INTEGER NOT NULL DEFAULT 0",
                "watch": "INTEGER NOT NULL DEFAULT 0",
                "health_url": "TEXT",
                "tests": "INTEGER NOT NULL DEFAULT 0",
                "deps": "INTEGER NOT NULL DEFAULT 0",
                "signals": "TEXT NOT NULL DEFAULT ''",
            },
        )
        self._add_columns(
            "prs",
            {
                "kind": "TEXT NOT NULL DEFAULT 'change'",
                "merge_sha": "TEXT",
                "watch_until": "REAL",
                "health_failures": "INTEGER NOT NULL DEFAULT 0",
                "reverted": "TEXT",
            },
        )

    def _add_columns(self, table: str, columns: Mapping[str, str]) -> None:
        """Columns that later versions added, for databases made before them."""
        have = {r["name"] for r in self.q(f"PRAGMA table_info({table})")}
        for name, declaration in columns.items():
            if name not in have:
                self.x(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def x(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.db.execute(sql, args)

    def q(self, sql: str, args: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # settings

    def setting(self, key: str, default: str = "") -> str:
        rows = self.q("SELECT value FROM settings WHERE key = ?", (key,))
        return str(rows[0]["value"]) if rows else default

    def set_setting(self, key: str, value: str) -> None:
        self.x(
            "INSERT INTO settings VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value", (key, value)
        )

    # accounts and projects

    def add_account(
        self,
        account_id: str,
        engine: str,
        language: str = "English",
        reserve_pct: float = 10,
        *,
        provider: str = "",
        base_url: str = "",
        models: Mapping[str, str] | None = None,
        policy: str = "before-reset",
        budget_usd: float | None = None,
        schedule: str = "",
    ) -> None:
        self.x(
            "INSERT INTO accounts (id, engine, language, reserve_pct, created, provider, base_url, models, policy, "
            "budget_usd, schedule) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO UPDATE SET engine = excluded.engine, language = excluded.language, "
            "reserve_pct = excluded.reserve_pct, provider = excluded.provider, base_url = excluded.base_url, "
            "models = excluded.models, policy = excluded.policy, budget_usd = excluded.budget_usd, "
            "schedule = excluded.schedule",
            (
                account_id,
                engine,
                language,
                reserve_pct,
                time.time(),
                provider,
                base_url,
                json.dumps(dict(models or {})),
                policy,
                budget_usd,
                schedule,
            ),
        )

    def update_account(
        self,
        account_id: str,
        *,
        enabled: bool,
        language: str,
        reserve_pct: float,
        policy: str | None = None,
        budget_usd: float | None = None,
        schedule: str = "",
        models: Mapping[str, str] | None = None,
    ) -> None:
        """The owner's settings. The policy, budget, schedule and models change only when `policy` is given."""
        self.x(
            "UPDATE accounts SET enabled = ?, language = ?, reserve_pct = ? WHERE id = ?",
            (int(enabled), language, reserve_pct, account_id),
        )
        if policy is not None:
            self.x(
                "UPDATE accounts SET policy = ?, budget_usd = ?, schedule = ? WHERE id = ?",
                (policy, budget_usd, schedule, account_id),
            )
        if models is not None:
            self.x("UPDATE accounts SET models = ? WHERE id = ?", (json.dumps(dict(models)), account_id))

    def set_tiers(self, account_id: str, models: Mapping[str, str], efforts: Mapping[str, str]) -> None:
        """The account's own model and effort of each tier, over the engine's defaults."""
        self.x(
            "UPDATE accounts SET models = ?, efforts = ? WHERE id = ?",
            (json.dumps(dict(models)), json.dumps(dict(efforts)), account_id),
        )

    def accounts(self, enabled_only: bool = False) -> list[AccountRow]:
        rows = self.q("SELECT * FROM accounts" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY id")
        return [
            AccountRow(
                r["id"],
                r["engine"],
                bool(r["enabled"]),
                r["language"],
                r["reserve_pct"],
                r["provider"],
                r["base_url"],
                json.loads(r["models"] or "{}"),
                r["policy"],
                r["budget_usd"],
                r["schedule"],
                json.loads(r["efforts"] or "{}"),
            )
            for r in rows
        ]

    def account(self, account_id: str) -> AccountRow | None:
        return next((a for a in self.accounts() if a.id == account_id), None)

    def add_project(self, repo: str, active: bool = True, weight: float = 1, language: str | None = None) -> None:
        self.x(
            "INSERT INTO projects (repo, active, weight, language, created) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (repo) DO UPDATE SET active = excluded.active, weight = excluded.weight, "
            "language = excluded.language",
            (repo, int(active), weight, language, time.time()),
        )

    def projects(self, active_only: bool = False) -> list[ProjectRow]:
        where = " WHERE active = 1 AND weight > 0" if active_only else ""
        rows = self.q("SELECT * FROM projects" + where + " ORDER BY repo")
        return [
            ProjectRow(
                r["repo"],
                bool(r["active"]),
                r["weight"],
                r["language"],
                r["max_prs"],
                bool(r["merge_on_approve"]),
                bool(r["watch"]),
                r["health_url"],
                bool(r["tests"]),
                bool(r["deps"]),
                r["signals"],
            )
            for r in rows
        ]

    def project(self, repo: str) -> ProjectRow | None:
        return next((p for p in self.projects() if p.repo.lower() == repo.lower()), None)

    def update_project(
        self,
        repo: str,
        *,
        active: bool,
        weight: float,
        language: str | None,
        max_prs: int,
        merge_on_approve: bool = False,
        watch: bool = False,
        health_url: str | None = None,
        tests: bool = False,
        deps: bool = False,
        signals: str = "",
    ) -> None:
        self.x(
            "UPDATE projects SET active = ?, weight = ?, language = ?, max_prs = ?, merge_on_approve = ?, watch = ?, "
            "health_url = ?, tests = ?, deps = ?, signals = ? WHERE repo = ?",
            (
                int(active),
                weight,
                language,
                max_prs,
                int(merge_on_approve),
                int(watch),
                health_url,
                int(tests),
                int(deps),
                signals,
                repo,
            ),
        )

    def set_signals(self, repo: str, project: str) -> None:
        """The project's project in the owner's error tracker; "" for none."""
        self.x("UPDATE projects SET signals = ? WHERE repo = ?", (project, repo))

    def pause_project(self, repo: str) -> None:
        """After a revert: no more bursts on the project until the owner activates it again."""
        self.x("UPDATE projects SET active = 0 WHERE repo = ?", (repo,))

    # suggestions for the installed repositories, refreshed now and then from GitHub

    def save_suggestions(self, found: Iterable[Suggestion], now: float) -> None:
        rows = [(s.repo, json.dumps(asdict(s)), now) for s in found]
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute("DELETE FROM suggestions")
                self.db.executemany("INSERT INTO suggestions VALUES (?, ?, ?)", rows)
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")
        self.set_setting("suggestions_refreshed", f"{now:.0f}")

    def suggestions(self) -> list[Suggestion]:
        found = []
        for r in self.q("SELECT data FROM suggestions"):
            data = json.loads(r["data"])
            found.append(Suggestion(**{**data, "why": tuple((k, v) for k, v in data["why"])}))
        return sorted(found, key=lambda s: (-s.score, s.repo))

    # orders from the owner, which the service carries out

    def add_order(self, kind: str, repo: str, account: str | None, now: float) -> None:
        self.x("INSERT INTO orders (kind, repo, account, created) VALUES (?, ?, ?, ?)", (kind, repo, account, now))

    def orders(self) -> list[dict[str, Any]]:
        """Orders not started yet, oldest first."""
        return self.q("SELECT * FROM orders WHERE started IS NULL ORDER BY id")

    def start_order(self, order_id: int, now: float) -> None:
        self.x("UPDATE orders SET started = ? WHERE id = ?", (now, order_id))

    # quota readings

    def record_reading(self, reading: Reading) -> None:
        windows = json.dumps([w.__dict__ for w in reading.windows])
        self.x(
            "INSERT INTO readings VALUES (?, ?, ?, ?, ?)",
            (reading.account, reading.taken_at, windows, int(reading.paid_usage), reading.fidelity),
        )

    def readings(self, account: str, since: float) -> list[Reading]:
        rows = self.q(
            "SELECT * FROM readings WHERE account = ? AND taken_at >= ? ORDER BY taken_at", (account, since)
        )
        return [_reading(r) for r in rows]

    def last_reading(self, account: str) -> Reading | None:
        rows = self.q("SELECT * FROM readings WHERE account = ? ORDER BY taken_at DESC LIMIT 1", (account,))
        return _reading(rows[0]) if rows else None

    # the ledger: what the steps of accounts on API keys and local models cost

    def record_spend(self, account: str, usd: float, role: str, run: str = "", at: float | None = None) -> None:
        self.x(
            "INSERT INTO spend (account, at, usd, run, role) VALUES (?, ?, ?, ?, ?)",
            (account, time.time() if at is None else at, usd, run, role),
        )

    def spent(self, account: str, since: float) -> float:
        """What the account's steps cost since then, in USD."""
        rows = self.q(
            "SELECT COALESCE(SUM(usd), 0) AS usd FROM spend WHERE account = ? AND at >= ?", (account, since)
        )
        return float(rows[0]["usd"])

    # runs

    def start_run(
        self, run: str, repo: str, account: str, trigger: str, crew: Sequence[str] = (), recipe: str = "audit"
    ) -> None:
        """`crew`: the other accounts whose reviewers take part (fan-out)."""
        self.x(
            "INSERT INTO runs (id, repo, account, trigger, status, started, crew, recipe) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?, ?)",
            (run, repo, account, trigger, time.time(), ",".join(crew), recipe),
        )

    def finish_run(self, run: str, status: str, sha: str | None) -> None:
        self.x("UPDATE runs SET status = ?, sha = ?, ended = ? WHERE id = ?", (status, sha, time.time(), run))

    def interrupt_running(self) -> int:
        """At start-up: runs that were going when lastcall stopped are not going any more."""
        return self.x(
            "UPDATE runs SET status = 'interrupted', ended = ? WHERE status = 'running'", (time.time(),)
        ).rowcount

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM runs ORDER BY started DESC LIMIT ?", (limit,))

    def running(self) -> list[dict[str, Any]]:
        return self.q("SELECT * FROM runs WHERE status = 'running' ORDER BY started")

    def latest_runs(self) -> dict[str, dict[str, Any]]:
        """The latest run of each repository, whatever its account and status."""
        rows = self.q("SELECT * FROM runs ORDER BY started")
        return {r["repo"]: r for r in rows}

    def last_run(self, repo: str, account: str) -> dict[str, Any] | None:
        """The account's last audit of the project that got somewhere."""
        rows = self.q(
            "SELECT * FROM runs WHERE repo = ? AND account = ? AND status IN ('ok', 'partial') AND recipe = 'audit' "
            "ORDER BY started DESC LIMIT 1",
            (repo, account),
        )
        return rows[0] if rows else None

    # findings and pull requests

    def record_findings(self, run: str, report: AuditReport) -> None:
        """The findings of a judged report, once. Later changes come from deliveries and from GitHub."""
        verdicts = {v["id"]: v for v in report.verdicts}
        for f in report.findings:
            if f["id"].startswith("#"):  # a finding from an earlier run: it already has its row
                continue
            v = verdicts.get(f["id"]) or {}
            if v.get("verdict") != "valid" or v.get("decision") == "discard":
                status = "discarded"
            else:
                status = "decide" if v.get("decision") == "issue" else "pending"
            self.x(
                "INSERT INTO findings (repo, run, fid, data, verdict, decision, severity, reason, status, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (run, fid) DO NOTHING",
                (
                    report.repo,
                    run,
                    f["id"],
                    json.dumps(f, ensure_ascii=False),
                    v.get("verdict"),
                    v.get("decision"),
                    v.get("severity", f.get("severity")),
                    v.get("reason"),
                    status,
                    time.time(),
                ),
            )

    def _record_pr(self, url: str, repo: str, run: str, title: str, kind: str = "change") -> None:
        number = int(url.rstrip("/").rsplit("/", 1)[1])
        now = time.time()
        self.x(
            "INSERT INTO prs (url, repo, number, run, title, state, created, updated, kind) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?) ON CONFLICT (url) DO NOTHING",
            (url, repo, number, run, title, now, now, kind),
        )

    def record_deliveries(self, run: str, report: AuditReport) -> None:
        """Opened pull requests and the decisions issue of a report."""
        for d in report.deliveries:
            if d.get("status") != "opened":
                continue
            url = str(d["url"])
            self._record_pr(url, report.repo, run, d["title"])
            for fid in d["ids"]:
                self._finding(run, fid, "UPDATE findings SET status = 'in_pr', pr_url = ? WHERE {where}", (url,))
        if report.issue_url:
            self.x(
                "UPDATE findings SET issue_url = ? WHERE run = ? AND status = 'decide' AND issue_url IS NULL",
                (report.issue_url, run),
            )

    def _finding(self, run: str, fid: str, sql: str, args: tuple[Any, ...]) -> None:
        if fid.startswith("#") and fid[1:].isdigit():
            self.x(sql.format(where="id = ?"), (*args, int(fid[1:])))
        else:
            self.x(sql.format(where="run = ? AND fid = ?"), (*args, run, fid))

    def earlier(self, repo: str, recipe: str = "audit") -> list[dict[str, Any]]:
        """What the judge must know from the earlier runs of the same recipe (the audit's, or the tests'), with ids
        #<n> (no reviewer prefix starts with #). Pending ones carry everything, because the judge may put them in a
        pull request of this run."""
        rows = self.q(
            "SELECT f.* FROM findings f LEFT JOIN runs r ON r.id = f.run WHERE f.repo = ? "
            f"AND COALESCE(r.recipe, 'audit') = ? AND f.status IN ({','.join('?' * len(REMEMBERED))}) ORDER BY f.id",
            (repo, recipe, *REMEMBERED),
        )
        out = []
        for r in rows:
            data = json.loads(r["data"])
            if r["status"] == "pending":
                out.append({**data, "id": f"#{r['id']}", "state": "pending"})
            else:
                keep = ("title", "file", "lines", "why")
                out.append({"id": f"#{r['id']}", "state": r["status"], **{k: data.get(k) for k in keep}})
        return out

    def open_prs(self, repo: str | None = None) -> list[dict[str, Any]]:
        if repo is None:
            return self.q("SELECT * FROM prs WHERE state = 'open' ORDER BY created")
        return self.q("SELECT * FROM prs WHERE state = 'open' AND repo = ? ORDER BY created", (repo,))

    def pending_count(self, repo: str) -> int:
        return int(
            self.q("SELECT count(*) AS n FROM findings WHERE repo = ? AND status = 'pending'", (repo,))[0]["n"]
        )

    def close_pr(self, url: str, merged: bool, merge_sha: str | None = None, watch_hours: float = 0) -> None:
        """A pull request closed on GitHub: its findings and ideas are merged or rejected. A merged one is watched
        for `watch_hours` when its project asks for it. The merge of a revert marks what it reverted `reverted`."""
        state, now = ("merged" if merged else "closed"), time.time()
        watch_until = now + watch_hours * 3600 if merged and merge_sha and watch_hours else None
        self.x(
            "UPDATE prs SET state = ?, updated = ?, merge_sha = ?, watch_until = ? WHERE url = ?",
            (state, now, merge_sha, watch_until, url),
        )
        for table in ("findings", "ideas", "tickets"):
            self.x(
                f"UPDATE {table} SET status = ? WHERE pr_url = ? AND status = 'in_pr'",
                ("merged" if merged else "rejected", url),
            )
        if merged:
            for row in self.q("SELECT url FROM prs WHERE reverted = ?", (url,)):
                for table in ("findings", "ideas", "tickets"):
                    sql = f"UPDATE {table} SET status = 'reverted' WHERE pr_url = ? AND status = 'merged'"
                    self.x(sql, (row["url"],))

    def reopen(self, url: str) -> None:
        """A pull request that lastcall closed because it no longer merges cleanly: its findings are pending again
        and its ideas wanted again, so that a later run makes the change on the current code."""
        self.x("UPDATE prs SET state = 'closed', updated = ? WHERE url = ?", (time.time(), url))
        self.x("UPDATE findings SET status = 'pending', pr_url = NULL WHERE pr_url = ? AND status = 'in_pr'", (url,))
        self.x("UPDATE ideas SET status = 'wanted', pr_url = NULL WHERE pr_url = ? AND status = 'in_pr'", (url,))
        self.x("UPDATE tickets SET status = 'waiting', pr_url = NULL WHERE pr_url = ? AND status = 'in_pr'", (url,))

    def watched(self, now: float) -> list[dict[str, Any]]:
        """Merged pull requests that lastcall still watches: not reverted, and within their window."""
        return self.q(
            "SELECT * FROM prs WHERE state = 'merged' AND kind = 'change' AND watch_until > ? AND reverted IS NULL "
            "ORDER BY updated",
            (now,),
        )

    def health(self, url: str, ok: bool) -> int:
        """Record one check of a watched pull request's health address; returns the failures in a row."""
        if ok:
            self.x("UPDATE prs SET health_failures = 0 WHERE url = ?", (url,))
            return 0
        self.x("UPDATE prs SET health_failures = health_failures + 1 WHERE url = ?", (url,))
        return int(self.q("SELECT health_failures FROM prs WHERE url = ?", (url,))[0]["health_failures"])

    def record_revert(self, url: str, revert_url: str | None) -> None:
        """The revert lastcall proposed for a merged pull request, or "" when it could not make one. Either way the
        watch is over. The revert is a lastcall pull request too, never watched itself."""
        self.x("UPDATE prs SET reverted = ? WHERE url = ?", (revert_url or "", url))
        if revert_url:
            original = self.q("SELECT repo, run, title FROM prs WHERE url = ?", (url,))[0]
            self._record_pr(revert_url, original["repo"], original["run"], f"Revert: {original['title']}", "revert")

    def open_issues(self) -> list[str]:
        return [
            r["issue_url"]
            for r in self.q(
                "SELECT DISTINCT issue_url FROM findings WHERE status = 'decide' AND issue_url IS NOT NULL"
            )
        ]

    def close_issue(self, url: str, completed: bool) -> int:
        status = "resolved" if completed else "dismissed"
        return self.x(
            "UPDATE findings SET status = ? WHERE issue_url = ? AND status = 'decide'", (status, url)
        ).rowcount

    def decisions(self) -> list[dict[str, Any]]:
        """Issues with findings that wait for the owner's decision: repo, issue_url and n."""
        return self.q(
            "SELECT repo, issue_url, count(*) AS n FROM findings WHERE status = 'decide' AND issue_url IS NOT NULL "
            "GROUP BY repo, issue_url ORDER BY min(created)"
        )

    # ideas

    def record_ideas(self, run: str, report: ImproveReport) -> None:
        """The ideas of an improvement run, once: those in its published plan are `proposed`, the rest `discarded`."""
        verdicts = {v["id"]: v for v in report.verdicts}
        planned = set(report.plan) if report.issue_url else set()
        for idea in report.ideas:
            status = "proposed" if idea["id"] in planned else "discarded"
            self.x(
                "INSERT INTO ideas (repo, run, iid, data, status, issue_url, created) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (run, iid) DO NOTHING",
                (
                    report.repo,
                    run,
                    idea["id"],
                    json.dumps({**idea, "verdict": verdicts.get(idea["id"])}, ensure_ascii=False),
                    status,
                    report.issue_url if status == "proposed" else None,
                    time.time(),
                ),
            )

    def earlier_ideas(self, repo: str) -> list[dict[str, Any]]:
        """What the ideator and the critic must know of earlier plans, with ids #<n>: the ideas the critic kept."""
        rows = self.q("SELECT * FROM ideas WHERE repo = ? AND status != 'discarded' ORDER BY id", (repo,))
        out = []
        for r in rows:
            data = json.loads(r["data"])
            keep = ("title", "kind", "change")
            out.append({"id": f"#{r['id']}", "state": r["status"], **{k: data.get(k) for k in keep}})
        return out

    def open_plans(self) -> list[str]:
        """The plans that still have ideas to tick or to build."""
        rows = self.q(
            "SELECT DISTINCT issue_url FROM ideas WHERE status IN ('proposed', 'wanted') AND issue_url IS NOT NULL"
        )
        return [r["issue_url"] for r in rows]

    def tick_ideas(self, url: str, ticks: Mapping[str, bool]) -> int:
        """The owner's ticks in an open plan: a ticked idea is `wanted`, an unticked one `proposed` again. Returns
        how many changed."""
        changed = 0
        for iid, on in ticks.items():
            new, old = ("wanted", "proposed") if on else ("proposed", "wanted")
            changed += self.x(
                "UPDATE ideas SET status = ? WHERE issue_url = ? AND iid = ? AND status = ?", (new, url, iid, old)
            ).rowcount
        return changed

    def close_plan(self, url: str, completed: bool) -> int:
        """A closed plan: the ideas it did not get ticked are dismissed. Closed as not planned, so are the ticked
        ones that are not built yet."""
        states = ("proposed",) if completed else ("proposed", "wanted")
        marks = ",".join("?" * len(states))
        return self.x(
            f"UPDATE ideas SET status = 'dismissed' WHERE issue_url = ? AND status IN ({marks})", (url, *states)
        ).rowcount

    def wanted_ideas(self, repo: str) -> list[dict[str, Any]]:
        """The ideas the owner ticked that no run has built yet, the oldest first, as their plan gave them, with
        `row` (their id here) and `plan` (the plan's URL)."""
        rows = self.q("SELECT * FROM ideas WHERE repo = ? AND status = 'wanted' ORDER BY id", (repo,))
        return [{**json.loads(r["data"]), "row": r["id"], "plan": r["issue_url"]} for r in rows]

    def wanted_count(self, repo: str) -> int:
        return int(self.q("SELECT count(*) AS n FROM ideas WHERE repo = ? AND status = 'wanted'", (repo,))[0]["n"])

    def record_idea_deliveries(self, run: str, report: ImproveReport) -> None:
        """What a run did with the ideas it built: an opened pull request makes its idea `in_pr`; an idea that could
        not be built is `stuck`, with why. One that a gate stopped stays `wanted`, for the next run."""
        for d in report.deliveries:
            status = d.get("status")
            if status == "opened":
                url = str(d["url"])
                self._record_pr(url, report.repo, run, d["title"])
                self.x("UPDATE ideas SET status = 'in_pr', pr_url = ? WHERE id = ?", (url, d["row"]))
            elif status not in (None, "stopped"):
                note = f"{status}: {d.get('note') or ''}"[:800]
                sql = "UPDATE ideas SET status = 'stuck', note = ? WHERE id = ? AND status = 'wanted'"
                self.x(sql, (note, d["row"]))

    # labelled issues

    def sync_tickets(self, repo: str, found: Sequence[Mapping[str, Any]]) -> int:
        """The issues labelled `lastcall` that are open now (`found`: number, url and title). A new one waits for a
        run; one that lastcall gave back, stuck or rejected and with its label taken off, waits again now that the
        owner labelled it again; one that waited and is not labelled and open any more is dropped. Returns how many
        changed."""
        changed, now, numbers = 0, time.time(), {int(i["number"]) for i in found}
        for issue in found:
            rows = self.q("SELECT * FROM tickets WHERE repo = ? AND number = ?", (repo, int(issue["number"])))
            if not rows:
                self.x(
                    "INSERT INTO tickets (repo, number, url, title, status, created) "
                    "VALUES (?, ?, ?, ?, 'waiting', ?)",
                    (repo, int(issue["number"]), str(issue["url"]), str(issue["title"]), now),
                )
                changed += 1
                continue
            given_back = rows[0]["status"] in ("stuck", "rejected") and rows[0]["unlabeled"]
            if rows[0]["status"] == "dropped" or given_back:
                self.x(
                    "UPDATE tickets SET status = 'waiting', title = ?, note = NULL, unlabeled = 0 WHERE id = ?",
                    (str(issue["title"]), rows[0]["id"]),
                )
                changed += 1
        for row in self.q("SELECT id, number FROM tickets WHERE repo = ? AND status = 'waiting'", (repo,)):
            if row["number"] not in numbers:
                self.x("UPDATE tickets SET status = 'dropped' WHERE id = ?", (row["id"],))
                changed += 1
        return changed

    def waiting_tickets(self, repo: str) -> list[dict[str, Any]]:
        """The labelled issues that wait for a run, the oldest first, with `row`, their id here."""
        rows = self.q("SELECT * FROM tickets WHERE repo = ? AND status = 'waiting' ORDER BY id", (repo,))
        return [{"row": r["id"], "number": r["number"], "url": r["url"], "title": r["title"]} for r in rows]

    def waiting_count(self, repo: str) -> int:
        return int(self.q("SELECT count(*) AS n FROM tickets WHERE repo = ? AND status = 'waiting'", (repo,))[0]["n"])

    def record_ticket_deliveries(self, run: str, report: IssuesReport) -> None:
        """What a run did with the labelled issues: an opened pull request makes its issue `in_pr`; an issue that
        could not be done is `stuck`, with why. One that a gate stopped still waits."""
        for d in report.deliveries:
            status = d.get("status")
            if status == "opened":
                url = str(d["url"])
                self._record_pr(url, report.repo, run, d["title"])
                self.x("UPDATE tickets SET status = 'in_pr', pr_url = ? WHERE id = ?", (url, d["row"]))
            elif status not in (None, "stopped"):
                note = f"{status}: {d.get('note') or ''}"[:800]
                sql = "UPDATE tickets SET status = 'stuck', note = ? WHERE id = ? AND status = 'waiting'"
                self.x(sql, (note, d["row"]))

    def to_unlabel(self) -> list[dict[str, Any]]:
        """Issues that lastcall gave back, stuck or rejected, whose label it has not taken off yet."""
        return self.q("SELECT * FROM tickets WHERE status IN ('stuck', 'rejected') AND unlabeled = 0 ORDER BY id")

    def unlabeled(self, row: int) -> None:
        self.x("UPDATE tickets SET unlabeled = 1 WHERE id = ?", (row,))

    def plans(self) -> list[dict[str, Any]]:
        """Open plans with ideas to choose from: repo, issue_url and n."""
        return self.q(
            "SELECT repo, issue_url, count(*) AS n FROM ideas WHERE status = 'proposed' AND issue_url IS NOT NULL "
            "GROUP BY repo, issue_url ORDER BY min(created)"
        )

    def statuses(self, repo: str) -> dict[str, int]:
        rows = self.q("SELECT status, count(*) AS n FROM findings WHERE repo = ? GROUP BY status", (repo,))
        return {r["status"]: int(r["n"]) for r in rows}

    # web UI sessions: the cookie holds a random token; the store keeps only its hash

    def add_session(self, token: str, csrf: str, now: float) -> None:
        self.x("INSERT INTO sessions VALUES (?, ?, ?, ?)", (token, csrf, now, now))

    def session(self, token: str) -> dict[str, Any] | None:
        rows = self.q("SELECT * FROM sessions WHERE token = ?", (token,))
        return rows[0] if rows else None

    def touch_session(self, token: str, now: float) -> None:
        self.x("UPDATE sessions SET seen = ? WHERE token = ?", (now, token))

    def drop_session(self, token: str) -> None:
        self.x("DELETE FROM sessions WHERE token = ?", (token,))

    def drop_sessions(self, seen_before: float | None = None) -> int:
        """Every session, or those idle since before `seen_before`."""
        if seen_before is None:
            return self.x("DELETE FROM sessions").rowcount
        return self.x("DELETE FROM sessions WHERE seen < ?", (seen_before,)).rowcount


def _reading(row: dict[str, Any]) -> Reading:
    windows = tuple(Window(**w) for w in json.loads(row["windows"]))
    return Reading(row["account"], windows, bool(row["paid"]), row["fidelity"], row["taken_at"])


def import_runs(store: Store, runs: Iterable[tuple[str, AuditReport, str]]) -> int:
    """Bring reports written before the store existed into it: (run name, report, account)."""
    count = 0
    for run, report, account in runs:
        if store.q("SELECT 1 FROM runs WHERE id = ?", (run,)):
            continue
        store.start_run(run, report.repo, account, "manual")
        store.finish_run(run, report.status, report.sha)
        store.record_findings(run, report)
        store.record_deliveries(run, report)
        count += 1
    return count
