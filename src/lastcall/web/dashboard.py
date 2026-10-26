"""What the dashboard shows, already in the owner's language: quota and next burst per account, what waits for the
owner, the projects and the recent runs. Templates only lay it out."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from lastcall import engines
from lastcall.engines.base import Keyed
from lastcall.i18n import LANGUAGES, MESSAGES, money, number, percent, plural, span, text
from lastcall.quota import BUDGET, Plan, Reading, Window, account_rate, ledger_reading, month, plan
from lastcall.runs import audit_models
from lastcall.service import DEFAULT_RATE, RATE_WINDOW
from lastcall.store import AccountRow, Store

HOUR = 3600.0
SERVICE_GONE = 5 * 60.0  # the service writes its heartbeat every minute
FINDING_ORDER = ("in_pr", "pending", "decide", "merged", "reverted", "resolved", "rejected", "dismissed", "discarded")


@dataclass(frozen=True)
class Step:
    text: str
    link: str  # the page of the web UI that does it, by endpoint
    label: str
    done: bool = False


@dataclass(frozen=True)
class WindowView:
    label: str
    used: float
    used_text: str
    resets: str
    role: str = "expiring"  # expiring, throttle or budget: picks the meter's colours and its help bubble
    model: bool = False  # the window limits one model only
    reserve: int = 0  # the owner's reserve, rounded to 5, for the marker on an expiring window


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
    engine_key: str = ""  # claude-code, codex, opencode: the engine's colour
    enabled: bool = True
    plan_code: str = ""  # the plan's code, for the account's state chip
    reviewing: bool = False  # busy as a reviewer of another account's run
    chip: str = ""  # "nokey" or "unreadable": the error that the state chip names instead of what it does
    trigger: str = ""  # what started the run it is in: "burst", or "manual" when the owner started it


@dataclass(frozen=True)
class Link:
    url: str
    text: str
    note: str = ""
    n: int = 1  # how many things it stands for: the findings of a decision issue, the ideas of a plan issue


@dataclass(frozen=True)
class ProjectView:
    repo: str
    state: str
    last: str
    last_status: str
    findings: str
    prs: int
    active: bool = False
    last_key: str = ""  # the last run's status: ok, failed... for its colour


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
    plans: list[Link] = field(default_factory=list)
    projects: list[ProjectView] = field(default_factory=list)
    runs: list[RunView] = field(default_factory=list)

    @property
    def waiting(self) -> int:
        """Everything that waits for the owner: pull requests, plus the findings and ideas their issues hold."""
        return len(self.prs) + sum(d.n for d in self.decisions) + sum(p.n for p in self.plans)


def service_state(store: Store, now: float) -> tuple[bool, bool]:
    """Whether bursts are paused and whether the service is gone (no heartbeat for a while). Either way, no card
    may say that an account is bursting."""
    heartbeat = float(store.setting("heartbeat") or 0)
    return store.setting("paused") == "yes", now - heartbeat >= SERVICE_GONE


def build(
    store: Store, lang: str, now: float, github_ready: bool, chips: Mapping[str, str] | None = None
) -> Dashboard:
    """`chips`: the error each account's state chip names, by account ("nokey", "unreadable")."""
    paused, down = service_state(store, now)
    heartbeat = float(store.setting("heartbeat") or 0)
    view = Dashboard(
        paused=paused,
        service=text(lang, "web.service.paused" if paused else "web.service.allowed"),
        down=down,
        seen=text(lang, "web.service.seen", when=ago(lang, heartbeat, now)) if heartbeat and down else "",
    )
    accounts = store.accounts()
    projects = store.projects()
    steps = [
        Step(text(lang, "web.start.github"), "github_page", text(lang, "web.github.title"), github_ready),
        Step(text(lang, "web.start.accounts"), "accounts_page", text(lang, "web.nav.accounts"), bool(accounts)),
        Step(
            text(lang, "web.start.projects"),
            "projects_page",
            text(lang, "web.nav.projects"),
            any(p.active for p in projects),
        ),
    ]
    if not all(step.done for step in steps):
        view.start = steps
    working = working_on(store.running())
    view.accounts = [
        account_view(store, a, lang, now, working.get(a.id), (chips or {}).get(a.id, "")) for a in accounts
    ]
    view.prs = [
        Link(safe_url(pr["url"]), pr["title"], f"{pr['repo']}#{pr['number']} · " + opened(lang, pr["created"], now))
        for pr in store.open_prs()
    ]
    view.decisions = [
        Link(
            safe_url(d["issue_url"]), plural(lang, "web.review.decide", int(d["n"]), repo=d["repo"]), "", int(d["n"])
        )
        for d in store.decisions()
    ]
    view.plans = [
        Link(safe_url(p["issue_url"]), plural(lang, "web.review.plan", int(p["n"]), repo=p["repo"]), "", int(p["n"]))
        for p in store.plans()
    ]
    latest = store.latest_runs()
    view.projects = [
        project_view(store, p.repo, p.active and p.weight > 0, p.weight, lang, now, latest.get(p.repo))
        for p in projects
    ]
    view.runs = [run_view(r, lang, now) for r in store.runs(10)]
    return view


def account_view(
    store: Store, row: AccountRow, lang: str, now: float, run: dict[str, Any] | None, chip: str = ""
) -> AccountView:
    """The account's card. `run`: the run it is in, if any. `chip`: the error its state chip names, if any; an account
    on an API key without its key cannot spend at all, so its plan says that instead of what the planner would do."""
    details = []
    if row.keyed:
        model, review = row.models.get("deep", ""), row.models.get("balanced", "")
        details.append(text(lang, "web.accounts.on", provider=provider_title(row)))
        if model:
            details.append(text(lang, "web.accounts.model", model=model))
        if review and review != model:
            details.append(text(lang, "web.accounts.review_model", model=review))
    details.append(text(lang, "web.accounts.language", name=language_name(lang, row.language)))
    if not row.keyed:
        details.append(text(lang, "web.accounts.reserve", pct=percent(lang, row.reserve_pct)))
    if not row.enabled:
        details.append(text(lang, "web.accounts.disabled"))
    line = " · ".join(details)
    line = line[:1].upper() + line[1:]
    reading = ledger(store, row, now) if row.keyed else store.last_reading(row.id)
    windows, read, plan_text, paid, code = [], text(lang, "web.accounts.never"), "", False, ""
    if reading:
        reserve = 0.0 if row.keyed else row.reserve_pct
        windows = [window_view(lang, w, now, row.budget_usd, reserve) for w in reading.windows]
        if row.keyed:
            read = "" if row.budget_usd else text(lang, "web.accounts.free")
        else:
            read = text(lang, "web.accounts.read", when=ago(lang, reading.taken_at, now))
        policy = row.spend_policy()
        history = [] if row.keyed else store.readings(row.id, now - RATE_WINDOW)
        rate = account_rate(history, reading, policy, DEFAULT_RATE)
        try:
            models = audit_models(row.account())
        except ValueError:  # an account that this version cannot run
            models = []
        planned = plan(reading, policy, rate, now, models)
        due = run is not None and run["trigger"] != "burst"  # a burst it plans starts when the owner's run ends
        plan_text = describe_plan(lang, planned, now, row.budget_usd, row.schedule, due)
        code, paid = planned.code, reading.paid_usage
    if chip == "nokey":
        plan_text, code = text(lang, "web.plan.no-key"), "no-key"
    working, reviewing = "", False
    if run is not None:
        when = ago(lang, run["started"], now)
        if run["account"] == row.id:
            working = text(lang, "web.accounts.working", repo=run["repo"], when=when)
        else:  # a peer of someone else's run
            working = text(lang, "web.accounts.reviewing", repo=run["repo"], lead=run["account"], when=when)
            reviewing = True
    title = engines.BUILTIN[row.engine]().title if row.engine in engines.BUILTIN else row.engine
    return AccountView(
        row.id,
        title,
        line,
        windows,
        read,
        plan_text,
        paid,
        working,
        row.engine,
        row.enabled,
        code,
        reviewing,
        chip,
        str(run["trigger"]) if run is not None else "",
    )


def ledger(store: Store, row: AccountRow, now: float) -> Reading:
    """The reading of an account on an API key or a local model: this month's spending, from the ledger."""
    return ledger_reading(row.id, row.budget_usd, store.spent(row.id, month(now)[0]), now)


def window_view(
    lang: str, w: Window, now: float, budget_usd: float | None = None, reserve_pct: float = 0.0
) -> WindowView:
    if not w.resets_at:
        resets = ""
    elif w.resets_at < now:
        resets = text(lang, "web.window.reset_passed")
    else:
        resets = text(lang, "web.window.resets", when=until(lang, w.resets_at, now))
    used_pct = min(100.0, w.used_pct)
    if w.name == BUDGET and budget_usd:
        spent = money(lang, w.used_pct * budget_usd / 100)
        used = text(lang, "web.window.spent", spent=spent, budget=money(lang, budget_usd))
        return WindowView(text(lang, "web.window.budget"), used_pct, used, resets, "budget")
    used = text(lang, "web.window.used", pct=percent(lang, w.used_pct))
    reserve = 0 if w.role == "throttle" else min(90, round(reserve_pct / 5) * 5)
    return WindowView(window_label(lang, w.name, w.model), used_pct, used, resets, w.role, bool(w.model), reserve)


def provider_title(row: AccountRow) -> str:
    """The provider of an account on a key as people know it: "OpenRouter"."""
    engine = engines.BUILTIN[row.engine]() if row.engine in engines.BUILTIN else None
    found: list[str] = (
        [p.title for p in engine.providers if p.name == row.provider] if isinstance(engine, Keyed) else []
    )
    return found[0] if found else row.provider


def working_on(running: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The run each busy account is in: its own, or another account's run that it reviews for."""
    busy: dict[str, dict[str, Any]] = {}
    for run in running:
        for peer in filter(None, str(run.get("crew") or "").split(",")):
            busy.setdefault(peer, run)
        busy[run["account"]] = run
    return busy


def describe_plan(
    lang: str, p: Plan, now: float, budget_usd: float | None = None, schedule: str = "", due: bool = False
) -> str:
    """The plan in the owner's words. `due`: the account is in a run that the owner started, so a burst that the plan
    calls for starts only when that run finishes (the service starts one run per account at a time)."""
    later = "due_" if due and p.code in ("now", "pace", "schedule") else ""
    leftover = percent(lang, max(0.0, p.leftover_pct))
    starts = until(lang, now + (p.starts_in_hours or 0) * HOUR, now)
    # a schedule never breaks across lines: no-break spaces between its fields, no-break hyphens in its ranges
    cron = schedule.replace(" ", "\u00a0").replace("-", "\u2011")
    if p.budget is not None and budget_usd and p.code in ("pace", "ahead", "spent"):
        spent, total = money(lang, p.budget.used_pct * budget_usd / 100), money(lang, budget_usd)
        resets = until(lang, p.budget.resets_at or now, now)
        return text(lang, f"web.plan.{later}{p.code}", spent=spent, budget=total, when=starts, resets=resets)
    match p.code:
        case "schedule" | "never" | "bad-schedule":
            return text(lang, f"web.plan.{later}{p.code}", cron=cron)
        case "window":
            return text(lang, "web.plan.window", when=starts, cron=cron)
        case "no-budget":
            return text(lang, "web.plan.no-budget")
        case "now":
            return text(
                lang, f"web.plan.{later}now", leftover=leftover, span=span(lang, (p.hours_to_reset or 0) * HOUR)
            )
        case "later":
            return text(lang, "web.plan.later", when=starts, leftover=leftover)
        case "small":
            return text(lang, "web.plan.small", leftover=leftover)
        case "model" if p.full:
            return text(lang, "web.plan.model", model=p.full.model, used=percent(lang, p.full.used_pct))
        case "paid" | "no-window" | "no-reset" | "manual" | "passed":
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
        active,
        last["status"] if last else "",
    )


def run_view(run: dict[str, Any], lang: str, now: float) -> RunView:
    started, ended = run["started"], run["ended"]
    return RunView(
        ago(lang, started, now),
        iso(started),
        run["repo"],
        run["account"],
        trigger(lang, run),
        label(lang, "web.run", run["status"]),
        run["status"],
        span(lang, ended - started) if ended else "",
    )


# formats


def ago(lang: str, when: float, now: float) -> str:
    if now - when < 60:
        return text(lang, "web.time.now")
    return text(lang, "web.time.ago", span=span(lang, now - when))


def until(lang: str, when: float, now: float) -> str:
    return text(lang, "web.time.in", span=span(lang, max(60.0, when - now)))


def opened(lang: str, when: float, now: float) -> str:
    return text(lang, "web.review.opened", when=ago(lang, when, now))


def window_label(lang: str, name: str, model: str = "") -> str:
    """The window's name in the owner's language, with the model when it limits only one."""
    key = f"web.window.{name}"
    label = text(lang, key) if key in MESSAGES["en"] else text(lang, "web.window.other", name=name)
    return text(lang, "web.window.model", window=label, model=model) if model else label


def language_name(lang: str, language: str) -> str:
    """An account's language in the owner's language, when the catalogs know it."""
    known = LANGUAGES.get(language.strip().lower())
    return text(lang, f"web.language.{known}") if known else language


def trigger(lang: str, run: dict[str, Any]) -> str:
    """What started a run, and the recipe when it is not an audit: "manual · improvement"."""
    started, recipe = label(lang, "web.trigger", run["trigger"]), run.get("recipe", "audit")
    return started if recipe == "audit" else f"{started} · {label(lang, 'web.recipe', recipe)}"


def label(lang: str, prefix: str, value: str) -> str:
    """The catalog's word for a state, or the state itself when the catalog does not know it."""
    key = f"{prefix}.{value}"
    return text(lang, key) if key in MESSAGES["en"] else value


def iso(when: float) -> str:
    return datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%d %H:%M UTC")


def safe_url(url: str) -> str:
    """Links to the forge only: anything that is not https becomes inert."""
    return url if url.startswith("https://") else "#"
