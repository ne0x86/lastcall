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
import math
import mimetypes
import time
from collections.abc import Callable, Sequence
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import waitress
from flask import Flask, Response, g, redirect, render_template, request, url_for
from flask.typing import ResponseReturnValue
from werkzeug.middleware.proxy_fix import ProxyFix

from lastcall import __version__, i18n
from lastcall.github import App
from lastcall.secretstore import SecretStore
from lastcall.store import Store
from lastcall.web import auth, dashboard

COOKIE = "lastcall_session"
LANGUAGE_COOKIE = "lastcall_language"
LANGUAGES = (("en", "English"), ("es", "Español"))
DOCS = "https://github.com/ne0x86/lastcall#readme"
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
) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # forms are small
    if trust_proxy:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]
    owner = auth.Owner(secrets)
    setup_code = auth.SetupCode(log)
    throttle = auth.Throttle(clock)
    if not owner.has_password():
        setup_code.issue()

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
        if response.mimetype == "text/html":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.context_processor
    def template_globals() -> dict[str, Any]:
        return {
            "t": t,
            "lang": g.get("lang", "en"),
            "languages": LANGUAGES,
            "csrf": g.session["csrf"] if g.get("session") else None,
            "version": __version__,
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
