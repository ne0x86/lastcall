"""The web UI: server-rendered pages that work without JavaScript, in English and Spanish (DESIGN.md, section 13).

`lastcall serve` serves it next to the service, and `lastcall web` serves it alone. One owner signs in
(`lastcall.web.auth`). Every POST must come from this site: its Origin (or, without one, its Referer) must be this
site, and a form sent while signed in must carry the session's CSRF token. Pages load no script, and a strict
Content-Security-Policy keeps it that way.

Behind a reverse proxy, set LASTCALL_TRUST_PROXY=1 so that lastcall reads the client's address and scheme from the
proxy's X-Forwarded-* headers, or list the public origin in LASTCALL_WEB_ORIGIN (https://lastcall.example.com).
"""

from __future__ import annotations

import hmac
import json
import math
import mimetypes
import re
import secrets as tokens
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import waitress
from flask import Flask, Response, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from werkzeug.middleware.proxy_fix import ProxyFix

from lastcall import __version__, cron, destinations, engines, i18n, logs, loki, projects, signals
from lastcall.accounts import ACCOUNT_ID, MODEL, Account, policies, tier_models
from lastcall.engines.base import Keyed
from lastcall.github import PERMISSIONS, App, Fetch, GitHub, GitHubError, urlopen
from lastcall.github_setup import LOGIN, Exchange, exchange_code, form_action, manifest
from lastcall.notify import EVENTS, Notifier, describe
from lastcall.quota import QuotaError, Reading
from lastcall.sandbox import SandboxError
from lastcall.secretstore import SecretStore
from lastcall.store import AccountRow, Store
from lastcall.supervisor import STEP
from lastcall.tiers import TIERS, defaults, efforts
from lastcall.web import auth, dashboard, project_views

COOKIE = "lastcall_session"
LANGUAGE_COOKIE = "lastcall_language"
LANGUAGES = (("en", "English"), ("es", "Español"))
DOCS = "https://github.com/ne0x86/lastcall#readme"
TERMS = "https://github.com/ne0x86/lastcall/blob/main/DESIGN.md#3-provider-terms-and-responsible-use"
CRON_HELP = "https://crontab.guru/"  # reads a cron expression back in words
SENTRY_TOKENS = "https://sentry.io/settings/account/api/auth-tokens/"  # where a Sentry user creates a token
ORG_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
TRACKER_PROJECT = re.compile(r"[A-Za-z0-9._-]{0,100}")
CONTAINER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
SERVICES = "https://github.com/caronc/apprise#supported-notifications"  # every service Apprise reaches, with its URL
STATE_SECONDS = 3600.0  # a GitHub App manifest form is good for an hour
QUICKSTART = "https://github.com/ne0x86/lastcall#-quickstart"
CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; manifest-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
Log = Callable[[str], None]
View = Callable[..., ResponseReturnValue]

mimetypes.add_type("application/manifest+json", ".webmanifest")


def create_app(
    store: Store,
    secrets: SecretStore,
    *,
    clock: Callable[[], float] = time.time,
    log: Log = print,
    trust_proxy: bool = False,
    origins: Sequence[str] = (),
    github: Callable[[], GitHub | None] | None = None,
    background: Callable[[Callable[[], None]], None] | None = None,
    notifier: Notifier | None = None,
    read_quota: Callable[[Account], Reading] | None = None,
    exchange: Exchange = exchange_code,
    fetch: Fetch = urlopen,
    signals_fetch: signals.Fetch = signals.fetch_json,
    containers: Callable[[], list[dict[str, Any]]] | None = None,
    loki_fetch: loki.Fetch = loki.fetch_json,
) -> Flask:
    """The web UI's application. `github` gives the GitHub client (by default, from the App in the secret store);
    `background` runs slow work, such as reading the installed repositories, outside the request. `read_quota`
    reads an account's quota in the sandbox; without it, the web UI cannot check a new account's login.
    `exchange` turns GitHub's one-time code into the App's credentials; `fetch` asks Telegram for a bot's chats."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # forms are small
    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    owner = auth.Owner(secrets)
    setup_code = auth.SetupCode(log)
    throttle = auth.Throttle(clock)
    if not owner.has_password():
        setup_code.issue()
    reading = threading.Event()  # the installed repositories are being read from GitHub
    notifications = notifier or Notifier(secrets, store, log=log)
    app_states: dict[str, float] = {}  # manifest forms shown, by state, with when they were shown
    quota_errors: dict[str, str] = {}  # the last failed quota reading of each account

    def connect() -> GitHub | None:
        if github is not None:
            return github()
        found = App.load(secrets)
        return GitHub(found) if found else None

    def later(work: Callable[[], None]) -> None:
        if background is not None:
            background(work)
        else:
            threading.Thread(target=work, name="web-refresh", daemon=True).start()

    def t(key: str, **values: object) -> str:
        return i18n.text(g.lang, key, **values)

    def page(template: str, status: int = 200, **values: Any) -> ResponseReturnValue:
        return render_template(template, **values), status

    def problem(status: int, key: str) -> ResponseReturnValue:
        return page("error.html", status, message=t(key), key=key)

    def signed_in(view: View) -> View:
        @wraps(view)
        def check(*args: Any, **kwargs: Any) -> ResponseReturnValue:
            if not owner.has_password():
                return redirect(url_for("setup"))
            if g.session is None:
                return redirect(url_for("login"))
            if request.method == "POST" and not same_token(request.form.get("csrf", ""), g.session["csrf"]):
                return problem(403, "web.error.csrf")
            return view(*args, **kwargs)

        return check

    def sign_in() -> ResponseReturnValue:
        token = auth.start_session(store, clock())
        response = redirect(url_for("home"))
        response.set_cookie(
            COOKIE,
            token,
            max_age=int(auth.SESSION_DAYS * auth.DAY),
            httponly=True,
            samesite="Lax",
            secure=request.is_secure,
        )
        return response

    def locked() -> str | None:
        wait = throttle.wait(address())
        return t("web.login.locked", minutes=math.ceil(wait / 60)) if wait > 0 else None

    @app.before_request
    def before() -> ResponseReturnValue | None:
        g.lang = language()
        g.session = None
        token = request.cookies.get(COOKIE)
        if token and request.endpoint != "static":
            g.session = auth.find_session(store, token, clock())
        if request.method == "POST" and not same_origin(origins):
            return problem(403, "web.error.origin")
        return None

    @app.after_request
    def headers(response: Response) -> Response:
        response.headers.update(HEADERS)
        if g.get("github_form"):  # the page whose form sends the App's manifest to GitHub
            response.headers["Content-Security-Policy"] = CSP.replace(
                "form-action 'self'", "form-action 'self' https://github.com"
            )
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        return {
            "t": t,
            "tn": lambda key, count, **values: i18n.plural(g.lang, key, count, **values),
            "lang": g.get("lang", "en"),
            "languages": LANGUAGES,
            "csrf": g.session["csrf"] if g.get("session") else None,
            "version": __version__,
            "nav": request.endpoint,
            "docs": DOCS,
            "quickstart": QUICKSTART,
        }

    @app.get("/")
    @signed_in
    def home() -> ResponseReturnValue:
        view = dashboard.build(store, g.lang, clock(), App.load(secrets) is not None)
        done = request.args.get("done", "")
        return page("dashboard.html", v=view, done=t(f"web.done.{done}") if done in ("paused", "resumed") else "")

    @app.route("/setup", methods=["GET", "POST"])
    def setup() -> ResponseReturnValue:
        if owner.has_password():
            return redirect(url_for("login"))
        setup_code.issue()  # after `lastcall password reset`, the log shows a new code
        if request.method == "GET":
            return page("setup.html", minimum=auth.MIN_LENGTH)
        if message := locked():
            return page("setup.html", 429, minimum=auth.MIN_LENGTH, error=message)
        if not setup_code.check(request.form.get("code", "")):
            throttle.fail(address())
            return page("setup.html", 400, minimum=auth.MIN_LENGTH, error=t("web.setup.bad_code"))
        password = request.form.get("password", "")
        wrong = auth.password_problem(password, request.form.get("repeat", ""))
        if wrong:
            key, values = wrong
            return page("setup.html", 400, minimum=auth.MIN_LENGTH, error=t(key, **values))
        owner.set_password(password)
        setup_code.clear()
        log("Web UI: the owner chose a password")
        return sign_in()

    @app.route("/login", methods=["GET", "POST"])
    def login() -> ResponseReturnValue:
        if not owner.has_password():
            return redirect(url_for("setup"))
        if g.session is not None:
            return redirect(url_for("home"))
        if request.method == "GET":
            return page("login.html")
        if message := locked():
            return page("login.html", 429, error=message)
        if not owner.check(request.form.get("password", "")):
            throttle.fail(address())
            return page("login.html", 401, error=t("web.login.wrong"))
        throttle.forget(address())
        return sign_in()

    @app.post("/logout")
    @signed_in
    def logout() -> ResponseReturnValue:
        store.drop_session(g.session["token"])
        response = redirect(url_for("login"))
        response.delete_cookie(COOKIE)
        return response

    @app.post("/service/<action>")
    @signed_in
    def service(action: str) -> ResponseReturnValue:
        if action not in ("pause", "resume"):
            return problem(404, "web.error.not_found")
        store.set_setting("paused", "yes" if action == "pause" else "no")
        return redirect(url_for("home", done="paused" if action == "pause" else "resumed"))

    @app.get("/projects")
    @signed_in
    def projects_page() -> ResponseReturnValue:
        return show_projects()

    def show_projects(status: int = 200, error: str = "", install: str = "") -> ResponseReturnValue:
        found = App.load(secrets)
        view = project_views.build(store, g.lang, clock(), found.install_url if found else "", reading.is_set())
        view.tracker = signals.Tracker.load(secrets) is not None
        done = request.args.get("done", "")
        known = ("added", "saved", "queued", "improve", "tests", "deps", "refreshing")
        message = t(f"web.done.{done}") if done in known and not error else ""
        return page("projects.html", status, v=view, done=message, error=error, install=install)

    @app.post("/projects/add")
    @signed_in
    def add_project() -> ResponseReturnValue:
        client = connect()
        if client is None:
            return show_projects(400, t("web.add.no_app"))
        repo = projects.parse_repo(request.form.get("repo", ""))
        if repo is None:
            return show_projects(400, t("web.add.bad"))
        try:
            name = projects.installed_name(client, repo)
        except GitHubError as e:
            if e.status == 404:
                return show_projects(400, t("web.add.not_installed", repo=repo), client.app.install_url)
            return show_projects(502, t("web.add.error", error=e))
        except OSError as e:
            return show_projects(502, t("web.add.error", error=e))
        if store.project(name) is None:
            store.add_project(name)
            log(f"Web UI: project {name} added")
        return redirect(url_for("projects_page", done="added"))

    @app.post("/projects/refresh")
    @signed_in
    def refresh_projects() -> ResponseReturnValue:
        client = connect()
        if client is None:
            return show_projects(400, t("web.add.no_app"))
        if not reading.is_set():
            reading.set()

            def work() -> None:
                try:
                    projects.refresh(store, client, clock())
                except (GitHubError, OSError) as e:
                    log(f"suggestions: {e}")
                finally:
                    reading.clear()

            later(work)
        return redirect(url_for("projects_page", done="refreshing"))

    def project_anchor(repo: str) -> str:
        """The card of a project on the Projects page: GitHub logins cannot hold two dashes in a row."""
        return "#project-" + repo.replace("/", "--", 1)

    @app.post("/projects/<owner>/<name>")
    @signed_in
    def project_settings(owner: str, name: str) -> ResponseReturnValue:
        project = store.project(f"{owner}/{name}")
        if project is None:
            return problem(404, "web.error.not_found")
        form = request.form
        language = form.get("language", "")
        try:
            weight, max_prs = float(form.get("weight", "")), int(form.get("max_prs", ""))
        except ValueError:
            return problem(400, "web.error.form")
        allowed = {"", *project_views.LANGUAGES, project.language or ""}
        health = form.get("health_url", "").strip()
        if not 0 < weight <= 10 or not 1 <= max_prs <= 10 or language not in allowed:
            return problem(400, "web.error.form")
        if health and (" " in health or not project_views.health_address(health)):
            return problem(400, "web.error.form")
        tracked = form.get("signals", project.signals).strip()  # the field shows only with a tracker connected
        if not TRACKER_PROJECT.fullmatch(tracked):
            return problem(400, "web.error.form")
        store.update_project(
            project.repo,
            active=form.get("active") == "1",
            weight=weight,
            language=language or None,
            max_prs=max_prs,
            merge_on_approve=form.get("merge_on_approve") == "1",
            watch=form.get("watch") == "1",
            health_url=health or None,
            tests=form.get("tests") == "1",
            deps=form.get("deps") == "1",
            signals=tracked,
        )
        return redirect(url_for("projects_page", done="saved") + project_anchor(project.repo))

    @app.post("/projects/<owner>/<name>/audit")
    @signed_in
    def audit_now(owner: str, name: str) -> ResponseReturnValue:
        project = store.project(f"{owner}/{name}")
        if project is None:
            return problem(404, "web.error.not_found")
        account = request.form.get("account", "")
        if account and account != "*" and account not in {a.id for a in store.accounts(enabled_only=True)}:
            return problem(400, "web.error.form")
        if not any(o["repo"].lower() == project.repo.lower() for o in store.orders()):
            store.add_order("audit", project.repo, account or None, clock())
            log(f"Web UI: audit of {project.repo} ordered")
        return redirect(url_for("projects_page", done="queued") + project_anchor(project.repo))

    @app.post("/projects/<owner>/<name>/improve")
    @signed_in
    def improve_now(owner: str, name: str) -> ResponseReturnValue:
        project = store.project(f"{owner}/{name}")
        if project is None:
            return problem(404, "web.error.not_found")
        account = request.form.get("account", "")
        if account and account != "*" and account not in {a.id for a in store.accounts(enabled_only=True)}:
            return problem(400, "web.error.form")
        if not any(o["repo"].lower() == project.repo.lower() for o in store.orders()):
            store.add_order("improve", project.repo, None if account in ("", "*") else account, clock())
            log(f"Web UI: improvement of {project.repo} ordered")
        return redirect(url_for("projects_page", done="improve") + project_anchor(project.repo))

    @app.post("/projects/<owner>/<name>/tests")
    @signed_in
    def tests_now(owner: str, name: str) -> ResponseReturnValue:
        return order(owner, name, "tests")

    @app.post("/projects/<owner>/<name>/deps")
    @signed_in
    def deps_now(owner: str, name: str) -> ResponseReturnValue:
        return order(owner, name, "deps")

    def order(owner: str, name: str, kind: str) -> ResponseReturnValue:
        """An order for one account of the project, or for any free one: it waits while another waits."""
        project = store.project(f"{owner}/{name}")
        if project is None:
            return problem(404, "web.error.not_found")
        account = request.form.get("account", "")
        if account and account != "*" and account not in {a.id for a in store.accounts(enabled_only=True)}:
            return problem(400, "web.error.form")
        if not any(o["repo"].lower() == project.repo.lower() for o in store.orders()):
            store.add_order(kind, project.repo, None if account in ("", "*") else account, clock())
            log(f"Web UI: {kind} of {project.repo} ordered")
        return redirect(url_for("projects_page", done=kind) + project_anchor(project.repo))

    @app.get("/settings")
    @signed_in
    def settings_page() -> ResponseReturnValue:
        return show_settings(service=request.args.get("service", ""))

    def show_settings(
        status: int = 200,
        error: str = "",
        details: str = "",
        service: str = "",
        values: Mapping[str, str] | None = None,
        chats: Sequence[tuple[str, str]] = (),
        signals_error: str = "",
        tried: Mapping[str, str] | None = None,
        loki_error: str = "",
        loki_tried: Mapping[str, str] | None = None,
    ) -> ResponseReturnValue:
        chosen, filled = notifications.events(), dict(values or {})
        language = (
            notifications.language() if store.setting("notify_language") else g.lang
        )  # until saved: this page's
        tracker = signals.Tracker.load(secrets)
        view = {
            "destinations": [(n, describe(url) or "?") for n, url in enumerate(notifications.urls())],
            "events": [(e, t(f"web.notify.event.{e}"), e in chosen) for e in EVENTS],
            "languages": [(code, name, code == language) for code, name in LANGUAGES],
            "services": [
                ("telegram", "Telegram"),
                ("ntfy", "ntfy"),
                ("discord", "Discord"),
                ("slack", "Slack"),
                ("email", t("web.dest.email.name")),
                ("apprise", t("web.dest.apprise.name")),
            ],
            "service": service if service in destinations.SERVICES else "",
            "filled": filled,  # not "values": in a template, v.values is the dict's method
            "topic": filled.get("topic") or destinations.random_topic(),
            "chats": list(chats),
            "fanout": store.setting("fanout") == "yes",
            "tracker": tracker,
            "logs": containers_view(),
            "loki": loki_view(loki_tried),
            "signals_url": (tried or {}).get("url") or (tracker.url if tracker else "https://sentry.io"),
            "signals_org": (tried or {}).get("org") or (tracker.org if tracker else ""),
        }
        done = request.args.get("done", "")
        known = (
            *("saved", "notify_added", "notify_removed", "notify_sent", "signals", "signals_removed", "logs"),
            *("loki", "loki_sources", "loki_label", "loki_removed"),
        )
        message = t(f"web.done.{done}") if done in known and not error else ""
        return page(
            "settings.html",
            status,
            v=view,
            done=message,
            error=error,
            details=details,
            github=App.load(secrets),
            services=SERVICES,
            signals_error=signals_error,
            sentry_tokens=SENTRY_TOKENS,
            loki_error=loki_error,
        )

    @app.post("/settings/signals")
    @signed_in
    def connect_signals() -> ResponseReturnValue:
        """Connect the owner's error tracker, once a request with the token gets through."""
        form = request.form
        url, org, token = form.get("url", "").strip().rstrip("/"), form.get("org", "").strip(), form.get("token", "")
        current = signals.Tracker.load(secrets)
        token = token.strip() or (current.token if current else "")
        tried = {"url": url, "org": org}
        if not signals.address(url) or not ORG_SLUG.fullmatch(org) or not token:
            return show_settings(400, signals_error=t("web.signals.bad"), tried=tried)
        tracker = signals.Tracker(url, org, token)
        try:
            signals.check(tracker, signals_fetch)
        except signals.SignalsError as e:
            return show_settings(400, signals_error=t("web.signals.failed", error=str(e)), tried=tried)
        tracker.save(secrets)
        log(f"Web UI: errors in production read from {url}, organization {org}")
        return redirect(url_for("settings_page", done="signals") + "#signals")

    @app.post("/settings/logs")
    @signed_in
    def logs_settings() -> ResponseReturnValue:
        """The project of each container, as the owner chooses it: lastcall reads the containers that have one."""
        form, repos = request.form, {p.repo for p in store.projects()}
        choices = logs.chosen(store)
        for key, name in form.items():
            if not key.startswith("name-"):
                continue
            index = key.removeprefix("name-")
            project = form.get("project-" + index, "")
            if not CONTAINER.fullmatch(name) or project not in {"", *repos}:
                return problem(400, "web.error.form")
            if project or form.get("shown-" + index):  # a project, or a no to the one that the page showed
                choices[name] = project
        logs.choose(store, choices)
        log(f"Web UI: containers read for their projects: {', '.join(sorted(k for k, v in choices.items() if v))}")
        return redirect(url_for("settings_page", done="logs") + "#logs")

    def containers_view() -> dict[str, Any]:
        """The containers of this host, each with its project, or the one lastcall suggests."""
        if containers is None:
            return {"error": t("web.logs.unavailable"), "rows": [], "repos": []}
        try:
            listed = containers()
        except Exception as e:  # an optional integration never takes the page down
            if not isinstance(e, (SandboxError, OSError)):
                log(f"Web UI: the containers could not be listed: {e!r}")
            return {"error": t("web.logs.failed", error=str(e)[:200]), "rows": [], "repos": []}
        repos, choices = [p.repo for p in store.projects()], logs.chosen(store)
        rows = []
        for c in listed:
            name = str(c.get("name") or "")
            choice = choices.get(name, "")
            hint = "" if c.get("label") or name in choices else logs.suggest(c, repos)
            rows.append({**c, "selected": choice or hint, "suggested": hint})
        return {"error": "", "rows": rows, "repos": repos}

    @app.post("/settings/loki")
    @signed_in
    def connect_loki() -> ResponseReturnValue:
        """Connect Loki, once a request with the login gets through."""
        form = request.form
        url, user = form.get("url", "").strip().rstrip("/"), form.get("user", "").strip()
        token, tenant = form.get("token", "").strip(), form.get("tenant", "").strip()
        current = loki.Loki.load(secrets)
        if not token and current is not None and (current.url, current.user) == (url, user):
            token = current.token  # the field is left empty to keep the saved one
        tried = {"url": url, "user": user, "tenant": tenant}
        if not signals.address(url) or ":" in user or len(user) > 100 or not loki.TENANT.fullmatch(tenant):
            return show_settings(400, loki_error=t("web.loki.bad"), loki_tried=tried)
        connection = loki.Loki(url, user, token, tenant)
        try:
            loki.labels(connection, clock(), loki_fetch)
        except loki.LokiError as e:
            return show_settings(400, loki_error=t("web.loki.failed", error=str(e)), loki_tried=tried)
        connection.save(secrets)
        log(f"Web UI: logs read from Loki at {url}")
        return redirect(url_for("settings_page", done="loki") + "#loki")

    @app.post("/settings/loki/label")
    @signed_in
    def loki_label() -> ResponseReturnValue:
        """The label that names Loki's sources. The owner's choices named sources by another one: they start again."""
        label = request.form.get("label", "")
        if not loki.LABEL_NAME.fullmatch(label):
            return problem(400, "web.error.form")
        if label != store.setting(loki.LABEL):
            store.set_setting(loki.LABEL, label)
            loki.choose(store, {})
        return redirect(url_for("settings_page", done="loki_label") + "#loki")

    @app.post("/settings/loki/sources")
    @signed_in
    def loki_sources() -> ResponseReturnValue:
        """The project of each source in Loki, as the owner chooses it: lastcall reads the sources that have one."""
        form, repos = request.form, {p.repo for p in store.projects()}
        label = form.get("label", "")
        if not loki.LABEL_NAME.fullmatch(label):
            return problem(400, "web.error.form")
        choices = loki.chosen(store) if label == store.setting(loki.LABEL) else {}
        for key, name in form.items():
            if not key.startswith("name-"):
                continue
            index = key.removeprefix("name-")
            project = form.get("project-" + index, "")
            if not loki.SOURCE.fullmatch(name) or name.startswith(STEP) or project not in {"", *repos}:
                return problem(400, "web.error.form")
            if project or form.get("shown-" + index):  # a project, or a no to the one that the page showed
                choices[name] = project
        store.set_setting(loki.LABEL, label)
        loki.choose(store, choices)
        read = ", ".join(sorted(k for k, v in choices.items() if v))
        log(f"Web UI: sources in Loki read for their projects: {read}")
        return redirect(url_for("settings_page", done="loki_sources") + "#loki")

    @app.post("/settings/loki/remove")
    @signed_in
    def disconnect_loki() -> ResponseReturnValue:
        secrets.delete(loki.SECRET)
        log("Web UI: Loki disconnected")
        return redirect(url_for("settings_page", done="loki_removed") + "#loki")

    def loki_view(tried: Mapping[str, str] | None) -> dict[str, Any]:
        """The connection to Loki, and its sources, each with its project, or the one lastcall suggests."""
        connection, tried = loki.Loki.load(secrets), tried or {}
        view: dict[str, Any] = {
            "connection": connection,
            "url": tried.get("url") or (connection.url if connection else ""),
            "user": tried.get("user") or (connection.user if connection else ""),
            "tenant": tried.get("tenant") or (connection.tenant if connection else ""),
            **{"error": "", "labels": [], "label": "", "rows": [], "repos": []},
        }
        if connection is None:
            return view
        repos, choices, now = [p.repo for p in store.projects()], loki.chosen(store), clock()
        try:
            names = loki.labels(connection, now, loki_fetch)
            label = loki.label_for(store, names)
            listed = loki.sources(connection, label, now, loki_fetch) if label else []
        except Exception as e:  # an optional integration never takes the page down
            if not isinstance(e, loki.LokiError):
                log(f"Web UI: Loki's sources could not be listed: {e!r}")
            return {**view, "error": t("web.loki.sources_failed", error=str(e)[:200])}
        rows = []
        for s in listed:
            name = s["name"]
            hint = "" if name in choices else logs.suggest(s, repos)
            rows.append({**s, "selected": choices.get(name, "") or hint, "suggested": hint})
        return {**view, "labels": names, "label": label, "rows": rows, "repos": repos}

    @app.post("/settings/signals/remove")
    @signed_in
    def disconnect_signals() -> ResponseReturnValue:
        secrets.delete(signals.SECRET)
        log("Web UI: error tracker disconnected")
        return redirect(url_for("settings_page", done="signals_removed") + "#signals")

    @app.post("/settings/notify/add")
    @signed_in
    def add_destination() -> ResponseReturnValue:
        service, form = request.form.get("service", ""), request.form
        try:
            url = destinations.apprise_url(service, form)
        except destinations.FormError as e:
            return show_settings(400, t(e.key, **e.values), service=service, values=form)
        if not store.setting("notify_language"):  # until the owner picks one: the language of this page
            notifications.choose(sorted(notifications.events()), g.lang)
        sent, said = notifications.test(url)
        if not sent:
            return show_settings(400, t("web.dest.failed"), said, service=service, values=form)
        notifications.add_url(url)
        log(f"Web UI: notification destination added ({service})")
        return redirect(url_for("settings_page", done="notify_added") + "#notify")

    @app.post("/settings/notify/telegram")
    @signed_in
    def find_telegram_chats() -> ResponseReturnValue:
        token = request.form.get("token", "").strip()
        try:
            chats = destinations.telegram_chats(token, fetch)
        except destinations.FormError as e:
            return show_settings(400, t(e.key, **e.values), service="telegram", values={"token": token})
        if not chats:
            return show_settings(error=t("web.dest.telegram.none"), service="telegram", values={"token": token})
        return show_settings(service="telegram", values={"token": token}, chats=chats)

    @app.post("/settings/notify/test/<int:index>")
    @signed_in
    def test_destination(index: int) -> ResponseReturnValue:
        urls = notifications.urls()
        if not 0 <= index < len(urls):
            return problem(404, "web.error.not_found")
        sent, said = notifications.test(urls[index])
        if not sent:
            return show_settings(502, t("web.dest.test_failed"), said)
        return redirect(url_for("settings_page", done="notify_sent") + "#notify")

    @app.post("/settings/notify/remove")
    @signed_in
    def remove_destination() -> ResponseReturnValue:
        try:
            notifications.remove_url(int(request.form.get("index", "")))
        except ValueError:
            return problem(400, "web.error.form")
        return redirect(url_for("settings_page", done="notify_removed") + "#notify")

    @app.post("/settings/runs")
    @signed_in
    def run_settings() -> ResponseReturnValue:
        store.set_setting("fanout", "yes" if request.form.get("fanout") == "1" else "no")
        return redirect(url_for("settings_page", done="saved") + "#runs")

    @app.post("/settings/notify")
    @signed_in
    def notify_settings() -> ResponseReturnValue:
        language = request.form.get("language", "")
        if language not in i18n.CATALOGS:
            return problem(400, "web.error.form")
        notifications.choose(request.form.getlist("events"), language)
        return redirect(url_for("settings_page", done="saved") + "#notify")

    @app.get("/github")
    @signed_in
    def github_page() -> ResponseReturnValue:
        found = App.load(secrets)
        done = t("web.done.app_created") if request.args.get("done") == "app_created" else ""
        return page("github.html", app=found, done=done)

    @app.get("/github/new")
    @signed_in
    def github_new() -> ResponseReturnValue:
        owner, org = request.args.get("owner", "").strip(), request.args.get("org") == "1"
        if not LOGIN.fullmatch(owner):
            return page("github.html", 400, app=None, error=t("web.github.bad_owner"), owner=owner)
        now = clock()
        for state, shown in list(app_states.items()):
            if now - shown > STATE_SECONDS:
                del app_states[state]
        state = tokens.token_urlsafe(16)
        app_states[state] = now
        values = manifest(owner, url_for("github_callback", _external=True))
        permissions = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in PERMISSIONS.items())
        g.github_form = True
        return page(
            "github.html",
            app=None,
            owner=owner,
            org=org,
            confirm=t("web.github.confirm", name=values["name"], owner=owner, permissions=permissions),
            action=form_action(owner, org, state),
            manifest=json.dumps(values),
        )

    @app.get("/github/callback")
    @signed_in
    def github_callback() -> ResponseReturnValue:
        code, state = request.args.get("code", ""), request.args.get("state", "")
        shown = app_states.pop(state, None)
        if not code or shown is None or clock() - shown > STATE_SECONDS:
            return page("github.html", 400, app=App.load(secrets), error=t("web.github.bad_state"))
        try:
            data = exchange(code)
            created = App(
                int(data["id"]),
                str(data["slug"]),
                str(data["name"]),
                str(data["owner"]["login"]),
                str(data["html_url"]),
                str(data["pem"]),
            )
        except (GitHubError, KeyError, TypeError, ValueError, OSError) as e:
            return page("github.html", 502, app=None, error=t("web.github.failed", error=e))
        created.save(secrets)
        log(f"Web UI: GitHub App {created.name} created")
        return redirect(url_for("github_page", done="app_created"))

    @app.get("/accounts")
    @signed_in
    def accounts_page() -> ResponseReturnValue:
        return show_accounts()

    def show_accounts(
        status: int = 200, error: str = "", chosen: str = "", filled: Mapping[str, str] | None = None
    ) -> ResponseReturnValue:
        """The accounts and the forms that add one. After a mistake in a form, `chosen` opens that form again with
        what the owner wrote in `filled` (never the API key)."""
        now = clock()
        working = dashboard.working_on(store.running())
        paused, down = dashboard.service_state(store, now)
        done = request.args.get("done", "")
        known = ("saved", "account_added", "key_added", "reading", "tiers")
        message = t(f"web.done.{done}") if done in known and not error else ""
        all_engines = [engines.get(name) for name in sorted(engines.BUILTIN)]
        sources = [(e, p) for e in all_engines if isinstance(e, Keyed) for p in e.providers]
        return page(
            "accounts.html",
            status,
            cards=[account_card(row, now, working) for row in store.accounts()],
            held="down" if down else ("paused" if paused else ""),
            engines=[(e.name, f"{e.provider} ({e.title})") for e in all_engines if e.login],
            keys=[(f"{e.name}/{p.name}", p) for e, p in sources if p.keys],
            local=next(((f"{e.name}/{p.name}", p) for e, p in sources if not p.keys), None),
            writing=[(name, dashboard.language_name(g.lang, name).capitalize()) for name in project_views.LANGUAGES],
            known=project_views.LANGUAGES,
            can_read=read_quota is not None,
            terms=TERMS,
            cron_help=CRON_HELP,
            done=message,
            error=error,
            chosen=chosen,
            filled={k: v for k, v in (filled or {}).items() if k not in ("key", "csrf")},
        )

    def account_card(row: AccountRow, now: float, working: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        try:
            base = defaults(row.engine)
        except ValueError:  # an engine with no defaults, such as opencode: the account names its models
            base = {}
        card: dict[str, Any] = {
            "row": row,
            "view": dashboard.account_view(store, row, g.lang, now, working.get(row.id)),
            "error": quota_errors.get(row.id, ""),
            "login": f"ACCOUNT={row.id} ENGINE={row.engine} docker compose run --rm login",
            "login_ps": f'$env:ACCOUNT="{row.id}"; $env:ENGINE="{row.engine}"; docker compose run --rm login',
            "policies": [],
            "needs_key": False,
            "levels": efforts(row.engine),
            "tiers": [
                {
                    "tier": tier,
                    "model": row.models.get(tier, ""),
                    "effort": row.efforts.get(tier, ""),
                    "default_model": base[tier][0] if tier in base else "",
                    "default_effort": (base[tier][1] or "") if tier in base else "",
                }
                for tier in TIERS
            ],
        }
        try:
            account = row.account()
        except ValueError:  # an account that this version cannot run, such as one of an engine that is gone
            return card
        card["policies"] = [(kind, t(f"web.spend.{kind}")) for kind in policies(account)]
        card["needs_key"] = account.needs_key()
        if account.needs_key() and secrets.get(account.key_name) is None:
            card["error"] = t("web.accounts.no_key")
        return card

    @app.post("/accounts/add")
    @signed_in
    def add_account() -> ResponseReturnValue:
        """An account on a subscription, which signs in with its CLI's own login."""
        form = request.form
        account_id = form.get("id", "").strip()
        if not ACCOUNT_ID.fullmatch(account_id):
            return show_accounts(400, t("web.accounts.bad"), "subscription", form)
        try:
            account = Account(account_id, form.get("engine", ""))
            reserve = float(form.get("reserve", "10"))
        except ValueError:
            return problem(400, "web.error.form")
        language = form.get("language", "English")
        if account.keyed or language not in project_views.LANGUAGES or not 0 <= reserve <= 90:
            return problem(400, "web.error.form")
        if store.account(account.id):
            return show_accounts(400, t("web.accounts.exists"), "subscription", form)
        store.add_account(account.id, account.engine, language, reserve)
        log(f"Web UI: account {account.id} ({account.engine}) added")
        return redirect(url_for("accounts_page", done="account_added"))

    @app.post("/accounts/add-key")
    @signed_in
    def add_keyed_account() -> ResponseReturnValue:
        """An account on an API key, or on a local model server."""
        form = request.form
        chosen = "local" if form.get("kind") == "local" else "key"
        account_id = form.get("id", "").strip()
        if not ACCOUNT_ID.fullmatch(account_id):
            return show_accounts(400, t("web.accounts.bad"), chosen, form)
        if store.account(account_id):
            return show_accounts(400, t("web.accounts.exists"), chosen, form)
        engine, _, provider = form.get("source", "").partition("/")
        found = spending(form, None, engine, provider, account_id)
        if isinstance(found, str):
            return show_accounts(400, found, chosen, form) if found else problem(400, "web.error.form")
        account, policy, schedule = found
        language = form.get("language", "English")
        if language not in project_views.LANGUAGES:
            return problem(400, "web.error.form")
        key = form.get("key", "").strip()
        if account.needs_key():
            secrets.set(account.key_name, key)
        store.add_account(
            account.id,
            account.engine,
            language,
            provider=account.provider,
            models=account.models,
            policy=policy,
            budget_usd=account.budget_usd,
            schedule=schedule,
        )
        log(f"Web UI: account {account.id} ({account.engine} on {account.provider}) added")
        return redirect(url_for("accounts_page", done="key_added"))

    def spending(
        form: Mapping[str, str], row: AccountRow | None, engine: str, provider: str, account_id: str
    ) -> tuple[Account, str, str] | str:
        """The account on a key that a form describes, with its policy and schedule; or what is wrong with the form,
        in the owner's language ("" when only a forged form could be wrong). The models of an account that exists
        change in its own form, Models and effort."""
        if row is not None:
            models = dict(row.models)
        else:
            model = form.get("model", "").strip()
            if not model:
                return t("web.accounts.need_model")
            models = tier_models(model, form.get("review_model", "").strip())
        try:
            account = Account(account_id, engine, provider, row.base_url if row else "", models)
        except ValueError:
            return ""
        budget = None
        if account.needs_key():
            try:
                budget = float(form.get("budget", ""))
            except ValueError:
                budget = None
            if budget is None or not math.isfinite(budget) or budget <= 0:
                return t("web.accounts.need_budget")
            if not form.get("key", "").strip() and (row is None or secrets.get(account.key_name) is None):
                return t("web.accounts.need_key")
        policy, schedule = form.get("policy", ""), form.get("schedule", "").strip()
        if policy not in policies(account):
            return ""
        if policy == "schedule":
            try:
                cron.parse(schedule)
            except ValueError:
                return t("web.accounts.bad_schedule")
        else:
            schedule = ""
        return replace(account, budget_usd=budget), policy, schedule

    @app.post("/accounts/<account_id>")
    @signed_in
    def account_settings(account_id: str) -> ResponseReturnValue:
        row = store.account(account_id)
        if row is None:
            return problem(404, "web.error.not_found")
        form = request.form
        language = form.get("language", "")
        if language not in {*project_views.LANGUAGES, row.language}:
            return problem(400, "web.error.form")
        enabled = form.get("enabled") == "1"
        if row.keyed:
            found = spending(form, row, row.engine, row.provider, row.id)
            if isinstance(found, str):
                return show_accounts(400, found) if found else problem(400, "web.error.form")
            account, policy, schedule = found
            key = form.get("key", "").strip()
            if key and account.needs_key():
                secrets.set(account.key_name, key)
            store.update_account(
                row.id,
                enabled=enabled,
                language=language,
                reserve_pct=row.reserve_pct,
                policy=policy,
                budget_usd=account.budget_usd,
                schedule=schedule,
                models=account.models,
            )
            return redirect(url_for("accounts_page", done="saved"))
        try:
            reserve = float(form.get("reserve", ""))
        except ValueError:
            return problem(400, "web.error.form")
        policy = form.get("policy", row.policy)
        if not 0 <= reserve <= 90 or policy not in ("before-reset", "manual"):
            return problem(400, "web.error.form")
        store.update_account(row.id, enabled=enabled, language=language, reserve_pct=reserve, policy=policy)
        return redirect(url_for("accounts_page", done="saved"))

    @app.post("/accounts/<account_id>/tiers")
    @signed_in
    def account_tiers(account_id: str) -> ResponseReturnValue:
        """The model and the effort of each tier for one account, over the engine's defaults: an empty field keeps
        the default."""
        row = store.account(account_id)
        if row is None:
            return problem(404, "web.error.not_found")
        form, levels = request.form, efforts(row.engine)
        models: dict[str, str] = {}
        chosen: dict[str, str] = {}
        for tier in TIERS:
            model, effort = form.get(f"model-{tier}", "").strip(), form.get(f"effort-{tier}", "")
            if (model and not MODEL.fullmatch(model)) or (effort and effort not in levels):
                return problem(400, "web.error.form")
            if model:
                models[tier] = model
            if effort:
                chosen[tier] = effort
        try:
            account = replace(row.account(), models=models, efforts=chosen)
        except ValueError:  # an engine with no defaults, such as opencode, needs a model for every tier
            return show_accounts(400, t("web.tiers.need_models"))
        store.set_tiers(row.id, account.models, account.efforts)
        used = ", ".join(f"{tier} {m}/{e or '-'}" for tier, (m, e) in account.tiers().items())
        log(f"Web UI: account {row.id} now runs {used}")
        return redirect(url_for("accounts_page", done="tiers") + f"#account-{row.id}")

    @app.post("/accounts/<account_id>/quota")
    @signed_in
    def read_account_quota(account_id: str) -> ResponseReturnValue:
        row = store.account(account_id)
        if row is None:
            return problem(404, "web.error.not_found")
        if read_quota is None:
            return show_accounts(503, t("web.accounts.no_sandbox"))
        try:
            account = row.account()
        except ValueError:
            return problem(400, "web.error.form")
        reader = read_quota

        def work() -> None:
            try:
                store.record_reading(reader(account))
                quota_errors.pop(account.id, None)
            except (QuotaError, SandboxError, OSError) as e:
                quota_errors[account.id] = str(e)[:300]
                log(f"{account.id}: no quota reading: {e}")

        later(work)
        return redirect(url_for("accounts_page", done="reading"))

    @app.get("/language/<code>")
    def set_language(code: str) -> ResponseReturnValue:
        response = redirect(local(request.args.get("next", "")))
        if code in i18n.CATALOGS:
            response.set_cookie(
                LANGUAGE_COOKIE, code, max_age=365 * 86_400, httponly=True, samesite="Lax", secure=request.is_secure
            )
        return response

    @app.get("/healthz")
    def health() -> ResponseReturnValue:
        return Response("ok\n", mimetype="text/plain")

    @app.errorhandler(404)
    def not_found(_: Exception) -> ResponseReturnValue:
        return problem(404, "web.error.not_found")

    return app


def language() -> str:
    chosen = request.cookies.get(LANGUAGE_COOKIE, "")
    if chosen in i18n.CATALOGS:
        return chosen
    return request.accept_languages.best_match(i18n.CATALOGS, default="en") or "en"


def address() -> str:
    return request.remote_addr or "unknown"


def same_origin(origins: Sequence[str]) -> bool:
    """Whether a POST comes from a page of this site: its Origin, or its Referer when a browser sends no Origin."""
    source = request.headers.get("Origin") or request.headers.get("Referer") or ""
    parts = urlsplit(source)
    if not parts.scheme or not parts.netloc:
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    return origin == f"{request.scheme}://{request.host}" or origin in origins


def same_token(given: str, expected: str) -> bool:
    return bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def local(target: str) -> str:
    """A path on this site to go back to, never another site."""
    parts = urlsplit(target)
    if not target.startswith("/") or target.startswith("//") or "\\" in target or parts.scheme or parts.netloc:
        return "/"
    return target


def serve(app: Flask, host: str, port: int, log: Log = print) -> None:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    log(f"Web UI on http://{shown}:{port}/ (in English and Spanish)")
    waitress.serve(app, host=host, port=port, threads=8, ident="lastcall")
