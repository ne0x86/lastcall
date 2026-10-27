"""Create the instance's GitHub App with GitHub's manifest flow: one click, permissions already filled in.

`lastcall github setup` serves a small page on this machine. Its button sends the manifest to GitHub; the owner
confirms there, and GitHub comes back to /callback with a one-time code that lastcall exchanges for the App's
credentials. The page needs no JavaScript.
"""

from __future__ import annotations

import html
import http.client
import json
import re
import secrets
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from lastcall.github import API, PERMISSIONS, App, Fetch, GitHubError, urlopen
from lastcall.i18n import text
from lastcall.secretstore import SecretStore

HOMEPAGE = "https://github.com/ne0x86/lastcall"
LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
PROFILE = re.compile(r"^(https?://)?(www\.)?github\.com/(orgs/(?=[^/\s]))?", re.IGNORECASE)
LOCAL_HOST = re.compile(r"(localhost|127\.0\.0\.1)(:\d{1,5})?")
LOOKUP_SECONDS = 10  # the owner lookup sits in a page load: a slow GitHub gets 10 s, then the owner is asked
Exchange = Callable[[str], dict[str, Any]]


def manifest(owner: str, redirect_url: str) -> dict[str, Any]:
    return {
        "name": f"lastcall-{owner}"[:34],  # GitHub App names: 34 characters at most, unique on GitHub
        "url": HOMEPAGE,
        "description": "Small, verified pull requests from the AI quota you already pay for.",
        "public": False,
        "hook_attributes": {"url": "https://example.invalid/lastcall-polls", "active": False},  # no webhooks
        "redirect_url": redirect_url,
        "default_permissions": PERMISSIONS,
        "default_events": [],
    }


def form_action(owner: str, org: bool, state: str) -> str:
    base = (
        f"https://github.com/organizations/{owner}/settings/apps/new"
        if org
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={urllib.parse.quote(state)}"


def clean_owner(typed: str) -> str:
    """What someone typed for the owner, without the decoration that people paste along with the name: spaces, a
    leading @, and the address of the profile (https://github.com/name/, github.com/name or an organization's
    https://github.com/orgs/name), whatever its case, with no query or fragment after it."""
    owner = re.split(r"[?#]", typed.strip(), maxsplit=1)[0].strip()
    owner = PROFILE.sub("", owner, count=1).strip().removeprefix("@")
    return owner.rstrip("/").strip()


def owner_kind(owner: str, fetch: Fetch = urlopen) -> str | None:
    """Ask GitHub's public API, with no token, what `owner` is: "user", "org" or "missing" (no such account). None
    when GitHub cannot say: the hourly limit of anonymous requests, no network, a connection that drops mid-answer
    or an answer that is not JSON."""
    req = urllib.request.Request(f"{API}/users/{urllib.parse.quote(owner, safe='')}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.timeout = LOOKUP_SECONDS  # read by github.urlopen; a fake fetch ignores it
    try:
        status, _, raw = fetch(req)
        if status == 404:
            return "missing"
        if status != 200:
            return None
        kind = json.loads(raw).get("type")
    except (OSError, ValueError, AttributeError, RecursionError, http.client.HTTPException):
        return None  # RecursionError: a body of nested brackets, which json.loads cannot read
    return {"User": "user", "Organization": "org"}.get(kind) if isinstance(kind, str) else None


def exchange_code(code: str) -> dict[str, Any]:
    req = urllib.request.Request(
        f"https://api.github.com/app-manifests/{urllib.parse.quote(code, safe='')}/conversions", method="POST"
    )
    req.add_header("Accept", "application/vnd.github+json")
    status, _, raw = urlopen(req)
    value = json.loads(raw) if raw.strip() else {}
    if status >= 400 or not isinstance(value, dict):
        raise GitHubError(status, value.get("message", "") if isinstance(value, dict) else "")
    return value


PAGE = """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>lastcall: create the GitHub App</title>
<body style="font-family:system-ui;max-width:40rem;margin:2rem auto;padding:0 1rem;line-height:1.5">
{body}
</body></html>"""


class SetupServer(HTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        store: SecretStore,
        owner: str,
        org: bool,
        exchange: Exchange = exchange_code,
    ) -> None:
        if not LOGIN.fullmatch(owner):
            raise ValueError(f"not a GitHub account or organization name: {owner!r}")
        super().__init__(address, _Handler)
        self.store, self.owner, self.org, self.exchange = store, owner, org, exchange
        self.state = secrets.token_urlsafe(16)
        self.app: App | None = None


class _Handler(BaseHTTPRequestHandler):
    server: SetupServer

    def do_GET(self) -> None:
        url = urllib.parse.urlparse(self.path)
        if url.path == "/":
            self._page(200, self._start())
        elif url.path == "/callback":
            self._callback(urllib.parse.parse_qs(url.query))
        else:
            self._page(404, "<p>Not found.</p>")

    def _start(self) -> str:
        srv = self.server
        host = self.headers.get("Host", "")
        if not LOCAL_HOST.fullmatch(host):
            host = f"127.0.0.1:{srv.server_address[1]}"
        app_manifest = json.dumps(manifest(srv.owner, f"http://{host}/callback"))
        signin = text("en", f"web.github.signin.{'org' if srv.org else 'user'}", owner=srv.owner)
        permissions = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in PERMISSIONS.items())
        return (
            f"<h1>Create lastcall's GitHub App</h1>"
            f"<p>GitHub will create a private App named <b>{html.escape(manifest(srv.owner, '')['name'])}</b> "
            f"for <b>{html.escape(srv.owner)}</b>, with these permissions: {html.escape(permissions)}. "
            f"No webhooks: lastcall asks GitHub when it needs to.</p>"
            f"<p>{html.escape(signin)}</p>"
            f'<form method="post" action="{html.escape(form_action(srv.owner, srv.org, srv.state))}">'
            f'<input type="hidden" name="manifest" value="{html.escape(app_manifest)}">'
            f'<button style="font-size:1.1rem;padding:.6rem 1.2rem">Create the App on GitHub</button></form>'
        )

    def _callback(self, query: dict[str, list[str]]) -> None:
        srv = self.server
        code, state = query.get("code", [""])[0], query.get("state", [""])[0]
        if not code or not secrets.compare_digest(state, srv.state):
            self._page(400, "<p>This answer does not come from the page lastcall served. Start again.</p>")
            return
        try:
            data = srv.exchange(code)
            app = App(
                int(data["id"]),
                str(data["slug"]),
                str(data["name"]),
                str(data["owner"]["login"]),  # for a person, whoever was signed in on GitHub
                str(data["html_url"]),
                str(data["pem"]),
            )
        except (GitHubError, KeyError, TypeError, ValueError) as e:
            self._page(502, f"<p>GitHub did not hand over the App: {html.escape(str(e))}</p>")
            return
        app.save(srv.store)
        srv.app = app
        self._page(
            200,
            f"<h1>App created</h1><p><b>{html.escape(app.name)}</b> is ready. Now install it on the repositories "
            f'lastcall may work on: <a href="{html.escape(app.install_url)}">install the App</a>. '
            f"You can choose all repositories or only some, and change it later.</p>",
        )

    def _page(self, status: int, body: str) -> None:
        data = PAGE.replace("{body}", body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action https:"
        )
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        pass


def run_setup(server: SetupServer) -> App:
    """Serve until GitHub hands over the App."""
    while server.app is None:
        server.handle_request()
    server.server_close()
    return server.app
