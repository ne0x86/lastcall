"""What the projects page shows, already in the owner's language: each project with its settings, and the
suggestions for the installed repositories that are not projects yet."""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lastcall.i18n import number, plural, text
from lastcall.store import ProjectRow, Store
from lastcall.suggest import Suggestion, explain
from lastcall.web.dashboard import ago, language_name, project_view

WEIGHTS = (("0.5", "low"), ("1", "normal"), ("2", "high"), ("3", "top"))
LANGUAGES = ("English", "Spanish")
MAX_PRS = range(1, 6)
# what a project's card says while an order of each recipe waits, or while a run of it works
QUEUED = {
    "audit": "web.project.queued",
    "improve": "web.project.queued.improve",
    "issues": "web.project.queued.issues",
    "tests": "web.project.queued.tests",
    "deps": "web.project.queued.deps",
}
RUNNING = {
    "audit": "web.project.running",
    "improve": "web.project.running.improve",
    "issues": "web.project.running.issues",
    "tests": "web.project.running.tests",
    "deps": "web.project.running.deps",
}


@dataclass(frozen=True)
class Option:
    value: str
    label: str
    selected: bool = False


@dataclass(frozen=True)
class ProjectCard:
    repo: str
    owner: str
    name: str
    url: str
    state: str  # "active · priority x2" or "inactive"
    on: bool  # the state says active: bursts may pick it
    last: str  # when it last ran: "47 h ago", or "never"
    last_status: str  # how that run ended, in words ("" when it never ran)
    last_key: str  # the raw status: ok, failed... for its colour
    findings: str
    prs: int
    tickets: str  # "2 labelled issues wait for a burst"; "" when none wait
    active: bool
    weights: list[Option]
    languages: list[Option]
    max_prs: list[Option]
    merge_on_approve: bool
    watch: bool
    health_url: str
    tests: bool
    deps: bool
    signals: str
    busy: str  # what it waits for or what runs on it, in words; "" when nothing does
    permissions_url: str  # where the owner gives the App what its merge or watch needs; "" when it has it


@dataclass(frozen=True)
class SuggestionView:
    repo: str
    url: str
    suggested: bool
    score: str
    language: str
    size: str  # "about 11k tokens of code", or "" when its size is unknown
    reasons: list[str]


@dataclass
class ProjectsPage:
    github_ready: bool
    install_url: str
    refreshed: str
    projects: list[ProjectCard] = field(default_factory=list)
    accounts: list[Option] = field(default_factory=list)
    suggested: list[SuggestionView] = field(default_factory=list)
    others: list[SuggestionView] = field(default_factory=list)
    all_added: bool = False
    tracker: bool = False  # the owner connected an error tracker: each project may name its project there

    @property
    def has_all(self) -> bool:
        """Whether the account choice offers "all free accounts": only with two or more enabled accounts."""
        return any(o.value == "*" for o in self.accounts)


def build(
    store: Store,
    lang: str,
    now: float,
    install_url: str,
    reading: bool,
    permissions_url: Callable[[str], str],
) -> ProjectsPage:
    """`permissions_url` says, for a repository, where its owner gives the App what reading checks takes: "" when the
    App has it already, or when that is not known. It is asked only for the projects that read checks."""
    projects = store.projects()
    busy = {o["repo"].lower(): text(lang, QUEUED.get(o["kind"], QUEUED["audit"])) for o in store.orders()}
    busy |= {r["repo"].lower(): text(lang, RUNNING.get(r["recipe"], RUNNING["audit"])) for r in store.running()}
    latest = store.latest_runs()
    page = ProjectsPage(bool(install_url), install_url, refreshed(store, lang, now, reading))
    page.projects = [
        card(
            store,
            p,
            lang,
            now,
            latest.get(p.repo),
            busy.get(p.repo.lower(), ""),
            permissions_url(p.repo) if p.merge_on_approve or p.watch else "",
        )
        for p in projects
    ]
    enabled = store.accounts(enabled_only=True)
    page.accounts = [Option("", text(lang, "web.project.any"), True)]
    if len(enabled) > 1:
        page.accounts.append(Option("*", text(lang, "web.project.all")))
    page.accounts += [Option(a.id, a.id) for a in enabled]
    names = {p.repo.lower() for p in projects}
    found = store.suggestions()
    left = [s for s in found if s.repo.lower() not in names]
    page.suggested = [suggestion_view(lang, s) for s in left if s.suggested]
    page.others = [suggestion_view(lang, s) for s in left if not s.suggested]
    page.all_added = bool(found) and not left
    return page


def card(
    store: Store, p: ProjectRow, lang: str, now: float, last: dict[str, Any] | None, busy: str, permissions_url: str
) -> ProjectCard:
    active = p.active and p.weight > 0
    view = project_view(store, p.repo, active, p.weight, lang, now, last)
    waiting = store.waiting_count(p.repo)
    weights = [Option(v, text(lang, f"web.project.weight.{k}"), float(v) == p.weight) for v, k in WEIGHTS]
    if not any(o.selected for o in weights):
        custom = text(lang, "web.project.weight.custom", weight=number(lang, p.weight))
        weights.append(Option(f"{p.weight:g}", custom, True))
    current = p.language or ""
    languages = [Option("", text(lang, "web.project.language.account"), not current)]
    for name in LANGUAGES:
        languages.append(Option(name, language_name(lang, name).capitalize(), current.lower() == name.lower()))
    if current and not any(o.selected for o in languages):
        languages.append(Option(current, current, True))
    max_prs = [Option(str(n), str(n), n == p.max_prs) for n in MAX_PRS]
    if p.max_prs not in MAX_PRS:
        max_prs.append(Option(str(p.max_prs), str(p.max_prs), True))
    owner, name = p.repo.split("/", 1)
    return ProjectCard(
        p.repo,
        owner,
        name,
        f"https://github.com/{p.repo}",
        view.state,
        active,
        view.last,
        view.last_status,
        view.last_key,
        view.findings,
        view.prs,
        plural(lang, "web.project.tickets", waiting) if waiting else "",
        p.active,
        weights,
        languages,
        max_prs,
        p.merge_on_approve,
        p.watch,
        p.health_url or "",
        p.tests,
        p.deps,
        p.signals,
        busy,
        permissions_url,
    )


def health_address(text: str) -> bool:
    """Whether the owner's health address is one lastcall can check: http or https, with a host."""
    parts = urllib.parse.urlsplit(text)
    return parts.scheme in ("http", "https") and bool(parts.netloc) and len(text) <= 300 and text.isprintable()


def suggestion_view(lang: str, s: Suggestion) -> SuggestionView:
    size = ""
    if s.code_tokens:
        thousands = s.code_tokens / 1000
        shown = number(lang, thousands if thousands < 10 else round(thousands))
        size = text(lang, "web.suggest.size", size=f"{shown}k")
    return SuggestionView(
        s.repo,
        f"https://github.com/{s.repo}",
        s.suggested,
        text(lang, "web.suggest.score", score=s.score),
        s.language or "",
        size,
        [explain(lang, r) for r in s.why],
    )


def refreshed(store: Store, lang: str, now: float, reading: bool) -> str:
    if reading:
        return text(lang, "web.suggest.reading")
    when = float(store.setting("suggestions_refreshed") or 0)
    return text(lang, "web.suggest.updated", when=ago(lang, when, now)) if when else text(lang, "web.suggest.never")
