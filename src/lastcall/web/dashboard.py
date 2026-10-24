"""What the dashboard shows, already in the owner's language: quota and next burst per account, what waits for the
owner, the projects and the recent runs. Templates only lay it out."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lastcall.i18n import LANGUAGES, MESSAGES, number, plural, text
from lastcall.quota import Plan, SpendPolicy, learned_rate, plan
from lastcall.service import DEFAULT_RATE, RATE_WINDOW
from lastcall.store import AccountRow, Store

HOUR = 3600.0
SERVICE_GONE = 5 * 60.0  # the service writes its heartbeat every minute
FINDING_ORDER = ("in_pr", "pending", "decide", "merged", "resolved", "rejected", "dismissed", "discarded")
ENGINES = {"claude-code": "Claude Code", "codex": "Codex"}


@dataclass(frozen=True)
class Step:
    text: str
    command: str = ""
    link: str = ""  # a page of the web UI, by endpoint


@dataclass(frozen=True)
class WindowView:
    label: str
    used: float
    used_text: str
    resets: str


@dataclass(frozen=True)
class AccountView:
    id: str
    engine: str
    details: str
    windows: list[WindowView]
    read: str
    plan: str
    paid: bool
    working: str


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    note: str = ""


@dataclass(frozen=True)
class ProjectView:
    repo: str
    state: str
    last: str
    last_status: str
    findings: str
    prs: int


@dataclass(frozen=True)
class RunView:
    started: str
    started_iso: str
    repo: str
    account: str
    trigger: str
    status: str
    status_key: str
    duration: str


@dataclass
class Dashboard:
    paused: bool
    service: str
    down: bool  # the service has not written its heartbeat for a while: nothing bursts
    seen: str  # when it was last seen, if ever
    start: list[Step] = field(default_factory=list)
    accounts: list[AccountView] = field(default_factory=list)
    prs: list[Link] = field(default_factory=list)
    decisions: list[Link] = field(default_factory=list)
    projects: list[ProjectView] = field(default_factory=list)
    runs: list[RunView] = field(default_factory=list)


def build(store: Store, lang: str, now: float, github_ready: bool) -> Dashboard:
    paused = store.setting("paused") == "yes"
    heartbeat = float(store.setting("heartbeat") or 0)
    alive = now - heartbeat < SERVICE_GONE
    view = Dashboard(
        paused=paused,
        service=text(lang, "web.service.paused" if paused else "web.service.allowed"),
        down=not alive,
        seen=text(lang, "web.service.seen", when=ago(lang, heartbeat, now)) if heartbeat and not alive else "",
    )
    accounts = store.accounts()
    projects = store.projects()
    if not github_ready:
        view.start.append(
            Step(
                text(lang, "web.start.github"),
                "docker compose run --rm -p 127.0.0.1:8765:8765 cli github setup --owner <login>",
            )
        )
    if not accounts:
        view.start.append(
            Step(
                text(lang, "web.start.accounts"),
                "ACCOUNT=claude-max docker compose run --rm login-claude-code\n"
                "docker compose run --rm cli account add claude-max --engine claude-code",
            )
        )
    if not any(p.active for p in projects):
        view.start.append(Step(text(lang, "web.start.projects"), link="projects_page"))
    working = {r["account"]: r for r in store.running()}
    view.accounts = [account_view(store, a, lang, now, working.get(a.id)) for a in accounts]
    view.prs = [
        Link(safe_url(pr["url"]), pr["title"], f"{pr['repo']}#{pr['number']} · " + opened(lang, pr["created"], now))
        for pr in store.open_prs()
    ]
    view.decisions = [
        Link(safe_url(d["issue_url"]), plural(lang, "web.review.decide", int(d["n"]), repo=d["repo"]))
        for d in store.decisions()
    ]
    latest = store.latest_runs()
    view.projects = [
        project_view(store, p.repo, p.active and p.weight > 0, p.weight, lang, now, latest.get(p.repo))
        for p in projects
    ]
    view.runs = [run_view(r, lang, now) for r in store.runs(10)]
    return view


def account_view(store: Store, row: AccountRow, lang: str, now: float, run: dict[str, Any] | None) -> AccountView:
    details = [
        text(lang, "web.accounts.language", name=language_name(lang, row.language)),
        text(lang, "web.accounts.reserve", pct=percent(lang, row.reserve_pct)),
    ]
    if not row.enabled:
        details.append(text(lang, "web.accounts.disabled"))
    line = " · ".join(details)
    line = line[:1].upper() + line[1:]
    reading = store.last_reading(row.id)
    windows, read, plan_text, paid = [], text(lang, "web.accounts.never"), "", False
    if reading:
        for w in reading.windows:
            resets = text(lang, "web.window.resets", when=until(lang, w.resets_at, now)) if w.resets_at else ""
            windows.append(
                WindowView(
                    window_label(lang, w.name),
                    w.used_pct,
                    text(lang, "web.window.used", pct=percent(lang, w.used_pct)),
                    resets,
                )
            )
        read = text(lang, "web.accounts.read", when=ago(lang, reading.taken_at, now))
        policy = SpendPolicy(reserve_pct=row.reserve_pct)
        rate = learned_rate(store.readings(row.id, now - RATE_WINDOW), policy, "week", "5h", DEFAULT_RATE)
        plan_text = describe_plan(lang, plan(reading, policy, rate, now), now)
        paid = reading.paid_usage
    working = ""
    if run is not None:
        working = text(lang, "web.accounts.working", repo=run["repo"], when=ago(lang, run["started"], now))
    return AccountView(row.id, ENGINES.get(row.engine, row.engine), line, windows, read, plan_text, paid, working)


def describe_plan(lang: str, p: Plan, now: float) -> str:
    leftover = percent(lang, max(0.0, p.leftover_pct))
    match p.code:
        case "now":
            return text(lang, "web.plan.now", leftover=leftover, span=span(lang, (p.hours_to_reset or 0) * HOUR))
        case "later":
            when = until(lang, now + (p.starts_in_hours or 0) * HOUR, now)
            return text(lang, "web.plan.later", when=when, leftover=leftover)
        case "small":
            return text(lang, "web.plan.small", leftover=leftover)
        case "paid" | "no-window" | "no-reset" | "manual":
            return text(lang, f"web.plan.{p.code}")
    return p.reason


def project_view(
    store: Store, repo: str, active: bool, weight: float, lang: str, now: float, last: dict[str, Any] | None
) -> ProjectView:
    counts = store.statuses(repo)
    findings = " · ".join(plural(lang, f"web.finding.{s}", counts[s]) for s in FINDING_ORDER if counts.get(s))
    state = text(lang, "web.projects.active", weight=number(lang, weight)) if active else ""
    return ProjectView(
        repo,
        state or text(lang, "web.projects.inactive"),
        ago(lang, last["started"], now) if last else text(lang, "web.projects.never"),
        label(lang, "web.run", last["status"]) if last else "",
        findings or text(lang, "web.projects.no_findings"),
        len(store.open_prs(repo)),
    )


def run_view(run: dict[str, Any], lang: str, now: float) -> RunView:
    started, ended = run["started"], run["ended"]
    return RunView(
        ago(lang, started, now),
        iso(started),
        run["repo"],
        run["account"],
        label(lang, "web.trigger", run["trigger"]),
        label(lang, "web.run", run["status"]),
        run["status"],
        span(lang, ended - started) if ended else "",
    )


# formats


def span(lang: str, seconds: float) -> str:
    s = abs(seconds)
    if s < HOUR:
        return text(lang, "web.time.min", n=max(1, round(s / 60)))
    if s < 48 * HOUR:
        return text(lang, "web.time.hours", n=round(s / HOUR))
    return plural(lang, "web.time.days", round(s / (24 * HOUR)))


def ago(lang: str, when: float, now: float) -> str:
    if now - when < 60:
        return text(lang, "web.time.now")
    return text(lang, "web.time.ago", span=span(lang, now - when))


def until(lang: str, when: float, now: float) -> str:
    return text(lang, "web.time.in", span=span(lang, max(60.0, when - now)))


def opened(lang: str, when: float, now: float) -> str:
    return text(lang, "web.review.opened", when=ago(lang, when, now))


def percent(lang: str, value: float) -> str:
    return text(lang, "web.percent", n=number(lang, value))


def window_label(lang: str, name: str) -> str:
    key = f"web.window.{name}"
    return text(lang, key) if key in MESSAGES["en"] else text(lang, "web.window.other", name=name)


def language_name(lang: str, language: str) -> str:
    """An account's language in the owner's language, when the catalogs know it."""
    known = LANGUAGES.get(language.strip().lower())
    return text(lang, f"web.language.{known}") if known else language


def label(lang: str, prefix: str, value: str) -> str:
    """The catalog's word for a state, or the state itself when the catalog does not know it."""
    key = f"{prefix}.{value}"
    return text(lang, key) if key in MESSAGES["en"] else value


def iso(when: float) -> str:
    return datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d %H:%M UTC")


def safe_url(url: str) -> str:
    """Links to the forge only: anything that is not https becomes inert."""
    return url if url.startswith("https://") else "#"
