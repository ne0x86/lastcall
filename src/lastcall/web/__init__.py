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
import secrets as tokens
import threading
import time
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import waitress
from flask import Flask, Response, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from werkzeug.middleware.proxy_fix import ProxyFix

from lastcall import __version__, engines, i18n, projects
from lastcall.accounts import Account
from lastcall.github import PERMISSIONS, App, GitHub, GitHubError
from lastcall.github_setup import LOGIN, Exchange, exchange_code, form_action, manifest
from lastcall.notify import EVENTS, Notifier, describe
from lastcall.quota import QuotaError, Reading
from lastcall.sandbox import SandboxError
from lastcall.secretstore import SecretStore
from lastcall.store import Store
from lastcall.web import auth, dashboard, project_views

COOKIE = "lastcall_session"
LANGUAGE_COOKIE = "lastcall_language"
LANGUAGES = (("en", "English"), ("es", "Español"))
DOCS = "https://github.com/ne0x86/lastcall#readme"
TERMS = "https://github.com/ne0x86/lastcall/blob/main/DESIGN.md#3-provider-terms-and-responsible-use"
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
) -> Flask:
    """The web UI's application. `github` gives the GitHub client (by default, from the App in the secret store);
    `background` runs slow work, such as reading the installed repositories, outside the request. `read_quota`
    reads an account's quota in the sandbox; without it, the web UI cannot check a new account's login.
    `exchange` turns GitHub's one-time code into the App's credentials."""
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
        return page("error.html", status, message=t(key))

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
        done = request.args.get("done", "")
        known = ("added", "saved", "queued", "refreshing")
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
        if not 0 < weight <= 10 or not 1 <= max_prs <= 10 or language not in allowed:
            return problem(400, "web.error.form")
        store.update_project(
            project.repo, active=form.get("active") == "1", weight=weight, language=language or None, max_prs=max_prs
        )
        return redirect(url_for("projects_page", done="saved"))

    @app.post("/projects/<owner>/<name>/audit")
    @signed_in
    def audit_now(owner: str, name: str) -> ResponseReturnValue:
        project = store.project(f"{owner}/{name}")
        if project is None:
            return problem(404, "web.error.not_found")
        account = request.form.get("account", "")
        if account and account not in {a.id for a in store.accounts(enabled_only=True)}:
            return problem(400, "web.error.form")
        if not any(o["repo"].lower() == project.repo.lower() for o in store.orders()):
            store.add_order("audit", project.repo, account or None, clock())
            log(f"Web UI: audit of {project.repo} ordered")
        return redirect(url_for("projects_page", done="queued"))

    @app.get("/settings")
    @signed_in
    def settings_page() -> ResponseReturnValue:
        return show_settings()

    def show_settings(status: int = 200, error: str = "") -> ResponseReturnValue:
        chosen, language = notifications.events(), notifications.language()
        view = {
            "destinations": [(n, describe(url) or "?") for n, url in enumerate(notifications.urls())],
            "events": [(e, t(f"web.notify.event.{e}"), e in chosen) for e in EVENTS],
            "languages": [(code, name, code == language) for code, name in LANGUAGES],
        }
        done = request.args.get("done", "")
        known = ("saved", "notify_added", "notify_removed", "notify_sent", "notify_failed")
        message = t(f"web.done.{done}") if done in known and not error else ""
        return page("settings.html", status, v=view, done=message, error=error, github=App.load(secrets))

    @app.post("/settings/notify/add")
    @signed_in
    def add_destination() -> ResponseReturnValue:
        if not notifications.add_url(request.form.get("url", "")):
            return show_settings(400, t("web.notify.bad_url"))
        if not store.setting("notify_language"):  # until the owner picks one: the language of this page
            notifications.choose(sorted(notifications.events()), g.lang)
        log("Web UI: notification destination added")
        return redirect(url_for("settings_page", done="notify_added"))

    @app.post("/settings/notify/remove")
    @signed_in
    def remove_destination() -> ResponseReturnValue:
        try:
            notifications.remove_url(int(request.form.get("index", "")))
        except ValueError:
            return problem(400, "web.error.form")
        return redirect(url_for("settings_page", done="notify_removed"))

    @app.post("/settings/notify")
    @signed_in
    def notify_settings() -> ResponseReturnValue:
        language = request.form.get("language", "")
        if language not in i18n.CATALOGS:
            return problem(400, "web.error.form")
        notifications.choose(request.form.getlist("events"), language)
        return redirect(url_for("settings_page", done="saved"))

    @app.post("/settings/notify/test")
    @signed_in
    def test_notification() -> ResponseReturnValue:
        sent = notifications.test()
        return redirect(url_for("settings_page", done="notify_sent" if sent else "notify_failed"))

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

    def show_accounts(status: int = 200, error: str = "") -> ResponseReturnValue:
        now = clock()
        working = {r["account"]: r for r in store.running()}
        cards = [
            {
                "row": row,
                "view": dashboard.account_view(store, row, g.lang, now, working.get(row.id)),
                "error": quota_errors.get(row.id, ""),
                "login": f"ACCOUNT={row.id} docker compose run --rm login-{row.engine}",
                "login_ps": f'$env:ACCOUNT="{row.id}"; docker compose run --rm login-{row.engine}',
            }
            for row in store.accounts()
        ]
        done = request.args.get("done", "")
        known = ("saved", "account_added", "reading")
        message = t(f"web.done.{done}") if done in known and not error else ""
        return page(
            "accounts.html",
            status,
            cards=cards,
            engines=[(name, t(f"web.engine.{name}")) for name in sorted(engines.BUILTIN)],
            writing=[(name, dashboard.language_name(g.lang, name).capitalize()) for name in project_views.LANGUAGES],
            known=project_views.LANGUAGES,
            can_read=read_quota is not None,
            terms=TERMS,
            done=message,
            error=error,
        )

    @app.post("/accounts/add")
    @signed_in
    def add_account() -> ResponseReturnValue:
        form = request.form
        try:
            account = Account(form.get("id", "").strip(), form.get("engine", ""))
            reserve = float(form.get("reserve", "10"))
        except ValueError:
            return show_accounts(400, t("web.accounts.bad"))
        language = form.get("language", "English")
        if language not in project_views.LANGUAGES or not 0 <= reserve <= 90:
            return problem(400, "web.error.form")
        if any(a.id == account.id for a in store.accounts()):
            return show_accounts(400, t("web.accounts.exists"))
        store.add_account(account.id, account.engine, language, reserve)
        log(f"Web UI: account {account.id} ({account.engine}) added")
        return redirect(url_for("accounts_page", done="account_added"))

    @app.post("/accounts/<account_id>")
    @signed_in
    def account_settings(account_id: str) -> ResponseReturnValue:
        row = next((a for a in store.accounts() if a.id == account_id), None)
        if row is None:
            return problem(404, "web.error.not_found")
        language = request.form.get("language", "")
        try:
            reserve = float(request.form.get("reserve", ""))
        except ValueError:
            return problem(400, "web.error.form")
        if language not in {*project_views.LANGUAGES, row.language} or not 0 <= reserve <= 90:
            return problem(400, "web.error.form")
        enabled = request.form.get("enabled") == "1"
        store.update_account(row.id, enabled=enabled, language=language, reserve_pct=reserve)
        return redirect(url_for("accounts_page", done="saved"))

    @app.post("/accounts/<account_id>/quota")
    @signed_in
    def read_account_quota(account_id: str) -> ResponseReturnValue:
        row = next((a for a in store.accounts() if a.id == account_id), None)
        if row is None:
            return problem(404, "web.error.not_found")
        if read_quota is None:
            return show_accounts(503, t("web.accounts.no_sandbox"))
        reader, account = read_quota, Account(row.id, row.engine)

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
