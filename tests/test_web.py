import html
import http.client
import json
import re
import threading
from collections.abc import Callable, Collection, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

import pytest
import waitress.server
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

import lastcall.web as site
from lastcall.accounts import Account
from lastcall.github import App, GitHub, Response
from lastcall.notify import Notifier
from lastcall.quota import HOUR, QuotaError, Reading, Window
from lastcall.recipes.audit import AuditReport
from lastcall.secretstore import SecretStore
from lastcall.store import Store
from lastcall.suggest import Suggestion
from lastcall.tiers import TIERS
from lastcall.web import CSP, GRANT_TIMEOUT, create_app, serve
from lastcall.web.auth import (
    IDLE_DAYS,
    LOCK_ALL,
    SECRET,
    SESSION_DAYS,
    Owner,
    Throttle,
    check_password,
    find_session,
    hash_password,
    start_session,
)
from test_loki import FakeLoki
from test_notify import Outbox
from test_projects import APP, FakeGitHub

NOW = 1_800_000_000.0
DAY = 86_400.0
SITE = {"Origin": "http://localhost"}
PASSWORD = "correct horse battery staple"
BOT = "123456789:AAHabcdefghijklmnopqrstuvwxyz0123456"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Web:
    def __init__(
        self, tmp_path: Path, origins: tuple[str, ...] = (), github: FakeGitHub | None = None, **options: Any
    ) -> None:
        self.clock = Clock()
        self.store = Store(tmp_path / "state" / "lastcall.db")
        self.secrets = SecretStore(tmp_path / "secrets")
        self.logs: list[str] = []
        if github is not None:
            github.app.save(self.secrets)
        self.outbox = Outbox()
        self.app = create_app(
            self.store,
            self.secrets,
            clock=self.clock,
            log=self.logs.append,
            origins=origins,
            github=(lambda: cast(GitHub, github)) if github else None,
            background=options.pop("background", lambda work: work()),
            notifier=Notifier(self.secrets, self.store, send=self.outbox, log=self.logs.append),
            **options,
        )
        self.client = self.app.test_client()

    def code(self) -> str:
        found = [m.group(1) for line in self.logs if (m := re.search(r"setup code: (\S+)", line))]
        return found[-1]

    def set_up(self) -> None:
        form = {"code": self.code(), "password": PASSWORD, "repeat": PASSWORD}
        assert self.client.post("/setup", data=form, headers=SITE).status_code == 302

    def csrf(self, client: FlaskClient | None = None) -> str:
        page = (client or self.client).get("/").get_data(as_text=True)
        found = re.search(r'name="csrf" value="([^"]+)"', page)
        assert found
        return found.group(1)


@pytest.fixture
def web(tmp_path: Path) -> Web:
    return Web(tmp_path)


def test_first_visit_asks_for_the_setup_code_from_the_log(web: Web) -> None:
    assert web.client.get("/").headers["Location"] == "/setup"
    page = web.client.get("/setup").get_data(as_text=True)
    assert "docker compose logs lastcall" in page
    form = {"code": "AAAA-AAAA", "password": PASSWORD, "repeat": PASSWORD}
    assert "setup code is not correct" in web.client.post("/setup", data=form, headers=SITE).get_data(as_text=True)
    form = {"code": web.code().lower(), "password": "short", "repeat": "short"}
    assert "at least 12 characters" in web.client.post("/setup", data=form, headers=SITE).get_data(as_text=True)
    form = {"code": web.code(), "password": PASSWORD, "repeat": PASSWORD + "!"}
    assert "not the same" in web.client.post("/setup", data=form, headers=SITE).get_data(as_text=True)

    form = {"code": web.code().replace("-", " "), "password": PASSWORD, "repeat": PASSWORD}
    response = web.client.post("/setup", data=form, headers=SITE)
    assert response.status_code == 302 and response.headers["Location"] == "/"
    cookie = response.headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    stored = web.secrets.get(SECRET) or ""
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert web.client.get("/").status_code == 200
    assert web.client.get("/setup").headers["Location"] == "/login"


def test_sign_in_and_out(web: Web) -> None:
    web.set_up()
    other = web.app.test_client()
    assert other.get("/").headers["Location"] == "/login"
    assert other.post("/login", data={"password": "wrong"}, headers=SITE).status_code == 401
    assert other.post("/login", data={"password": PASSWORD}, headers=SITE).status_code == 302
    assert other.get("/").status_code == 200

    assert other.post("/logout", headers=SITE).status_code == 403  # no CSRF token
    response = other.post("/logout", data={"csrf": web.csrf(other)}, headers=SITE)
    assert response.headers["Location"] == "/login"
    assert other.get("/").headers["Location"] == "/login"
    assert web.client.get("/").status_code == 200  # the other session goes on


def test_failed_attempts_lock_the_address(web: Web) -> None:
    web.set_up()
    other = web.app.test_client()
    for _ in range(5):
        assert other.post("/login", data={"password": "wrong"}, headers=SITE).status_code == 401
    locked = other.post("/login", data={"password": PASSWORD}, headers=SITE)
    assert locked.status_code == 429 and "Try again in 15 min" in locked.get_data(as_text=True)
    web.clock.now += 15 * 60
    assert other.post("/login", data={"password": PASSWORD}, headers=SITE).status_code == 302


def test_wrong_setup_codes_lock_too(web: Web) -> None:
    for _ in range(5):
        form = {"code": "AAAA-AAAA", "password": PASSWORD, "repeat": PASSWORD}
        assert web.client.post("/setup", data=form, headers=SITE).status_code == 400
    form = {"code": web.code(), "password": PASSWORD, "repeat": PASSWORD}
    assert web.client.post("/setup", data=form, headers=SITE).status_code == 429


def test_posts_need_this_sites_origin(tmp_path: Path) -> None:
    web = Web(tmp_path, origins=("https://lastcall.example.com",))
    web.set_up()
    other = web.app.test_client()
    assert other.post("/login", data={"password": PASSWORD}).status_code == 403
    for origin in ("http://evil.example", "null"):
        assert other.post("/login", data={"password": PASSWORD}, headers={"Origin": origin}).status_code == 403
    referer = {"Referer": "http://localhost/login"}  # browsers that send no Origin
    assert other.post("/login", data={"password": "wrong"}, headers=referer).status_code == 401
    proxied = {"Origin": "https://lastcall.example.com"}
    assert other.post("/login", data={"password": PASSWORD}, headers=proxied).status_code == 302


def test_only_a_trusted_proxy_says_the_scheme_and_the_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the real server, whose X-Forwarded-* headers test_client never drops: without LASTCALL_TRUST_PROXY a
    client cannot pass for HTTPS, and behind a trusted proxy the owner signs up over HTTPS with a Secure cookie, and
    GitHub brings the owner back to the public address."""
    servers: list[tuple[Any, threading.Thread]] = []

    def start(app: Any, **options: Any) -> None:  # waitress.serve, on a free port and in the background
        server = waitress.server.create_server(app, **{**options, "port": 0})
        servers.append((server, threading.Thread(target=server.run, daemon=True)))
        servers[-1][1].start()

    monkeypatch.setattr(waitress, "serve", start)
    proxy = {"Host": "lastcall.example.com", "X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}
    for trusted in (False, True):
        web = Web(tmp_path / f"trusted-{trusted}", trust_proxy=trusted, fetch=github_users({"acme": "User"}))
        serve(web.app, "127.0.0.1", 8080, log=web.logs.append, trust_proxy=trusted)
        server, thread = servers[-1]
        connection = http.client.HTTPConnection("127.0.0.1", server.effective_port, timeout=10)
        form = urlencode({"code": web.code(), "password": PASSWORD, "repeat": PASSWORD})
        headers = {**proxy, "Origin": "https://lastcall.example.com"}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection.request("POST", "/setup", body=form, headers=headers)
        response = connection.getresponse()
        response.read()
        if not trusted:
            assert response.status == 403
        else:
            cookie = response.getheader("Set-Cookie") or ""
            assert response.status == 302 and "Secure" in cookie
            connection.request("GET", "/github/new?owner=acme", headers={**proxy, "Cookie": cookie.split(";")[0]})
            page = connection.getresponse().read().decode()
            assert "https://lastcall.example.com/github/callback" in page
        connection.close()
        server.trigger.pull_trigger(server.close)  # closed from its own thread, never in the middle of a select()
        thread.join(10)
        assert not thread.is_alive()


def test_pause_and_resume_need_the_csrf_token(web: Web) -> None:
    web.set_up()
    assert web.client.post("/service/pause", data={"csrf": "forged"}, headers=SITE).status_code == 403
    assert web.store.setting("paused") == ""
    response = web.client.post("/service/pause", data={"csrf": web.csrf()}, headers=SITE)
    assert response.headers["Location"] == "/?done=paused"
    assert web.store.setting("paused") == "yes"
    page = web.client.get("/?done=paused").get_data(as_text=True)
    assert "Paused. No burst starts until you resume." in page and "Bursts: paused." in page
    web.client.post("/service/resume", data={"csrf": web.csrf()}, headers=SITE)
    assert web.store.setting("paused") == "no"


def test_every_page_sends_the_security_headers(web: Web) -> None:
    for path in ("/setup", "/nowhere"):
        response = web.client.get(path)
        assert response.headers["Content-Security-Policy"] == CSP
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Cache-Control"] == "no-store"
        assert "<script" not in response.get_data(as_text=True)
    assert web.client.get("/nowhere").status_code == 404
    assert web.client.get("/healthz").get_data(as_text=True) == "ok\n"


def test_language_comes_from_the_browser_or_the_owners_choice(web: Web) -> None:
    page = web.client.get("/setup", headers={"Accept-Language": "es-ES,es;q=0.9,en;q=0.8"}).get_data(as_text=True)
    assert '<html lang="es">' in page and "Configura lastcall" in page
    response = web.client.get("/language/en?next=/setup")
    assert response.headers["Location"] == "/setup"
    page = web.client.get("/setup", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Set up lastcall" in page
    for target in ("https://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)"):
        assert web.client.get("/language/es", query_string={"next": target}).headers["Location"] == "/"


def seed(store: Store) -> None:
    store.add_account("claude", "claude-code", "Spanish", 10)
    store.record_reading(
        Reading(
            "claude",
            (
                Window("5h", 40, NOW + 2 * HOUR, "throttle", 5.0),
                Window("week", 62, NOW + 30 * HOUR, "expiring", 168.0),
            ),
            False,
            "exact",
            NOW - 600,
        )
    )
    store.add_project("ne0x86/demo")
    store.start_run("demo-1", "ne0x86/demo", "claude", "burst")
    store.finish_run("demo-1", "ok", "a2051bd")
    report = AuditReport("ne0x86/demo", "a2051bd", "claude-code", "Spanish")
    finding = {"title": "T", "severity": "high", "file": "a.py", "lines": "1", "why": "w", "fix": "f"}
    report.findings = [{**finding, "id": "B1"}, {**finding, "id": "B2"}, {**finding, "id": "B3"}]
    report.verdicts = [
        {"id": "B1", "verdict": "valid", "decision": "pr"},
        {"id": "B2", "verdict": "valid", "decision": "issue"},
        {"id": "B3", "verdict": "false_positive", "decision": "discard"},
    ]
    report.deliveries = [
        {"status": "opened", "url": "https://github.com/ne0x86/demo/pull/7", "title": "Fix <b>it</b>", "ids": ["B1"]}
    ]
    report.issue_url = "https://github.com/ne0x86/demo/issues/8"
    store.record_findings("demo-1", report)
    store.record_deliveries("demo-1", report)


def test_dashboard_in_spanish_and_english(web: Web) -> None:
    seed(web.store)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Ventana semanal" in page and "62\u00a0% usado" in page
    assert "se reinicia en 30\u00a0h" in page
    assert "Próxima ráfaga en 14\u00a0h, para gastar 28\u00a0%." in page  # 28% left at 2%/h: 16 h before the reset
    assert "Fix &lt;b&gt;it&lt;/b&gt;" in page  # titles come from agents: escaped
    assert "1 decisión en ne0x86/demo" in page and 'href="https://github.com/ne0x86/demo/issues/8"' in page
    assert "1 en revisión · 1 por decidir · 1 descartado" in page
    assert "Cuota leída hace 10\u00a0min." in page and "terminada" in page
    assert "El servicio no está en marcha" not in page

    web.client.get("/language/en")
    page = web.client.get("/").get_data(as_text=True)
    assert "Weekly window" in page and "62% used" in page and "Next burst in 14\u00a0h, to spend 28%." in page
    assert "Writes in Spanish · reserve 10%" in page and "1 in review · 1 to decide · 1 discarded" in page
    assert "Getting started" in page  # no GitHub App yet


def test_a_reverted_finding_shows_in_the_findings_summary(web: Web) -> None:
    seed(web.store)
    web.store.x("UPDATE findings SET status = 'reverted' WHERE fid = 'B1'")
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "1 por decidir · 1 revertido · 1 descartado" in page
    page = web.client.get("/").get_data(as_text=True)
    assert "1 to decide · 1 reverted · 1 discarded" in page


def test_dashboard_says_when_the_service_is_not_running(web: Web) -> None:
    web.set_up()
    page = web.client.get("/").get_data(as_text=True)
    assert "The service is not running" in page and "Last seen" not in page
    assert "docker compose restart lastcall" in page and 'popovertarget="help-common-terminal"' in page
    web.store.set_setting("heartbeat", str(NOW - 3 * HOUR))
    page = web.client.get("/").get_data(as_text=True)
    assert "Last seen 3\u00a0h ago." in page and "docker compose restart lastcall" in page
    web.store.set_setting("heartbeat", str(NOW - 60))
    page = web.client.get("/").get_data(as_text=True)
    assert "The service is not running" not in page and "docker compose restart lastcall" not in page


def test_links_that_are_not_https_are_inert(web: Web) -> None:
    seed(web.store)
    web.store.x("UPDATE prs SET url = 'javascript:alert(1)'")
    web.set_up()
    page = web.client.get("/").get_data(as_text=True)
    assert "javascript:" not in page


def test_password_reset_brings_back_the_setup_code(web: Web) -> None:
    web.set_up()
    first = web.code()
    Owner(web.secrets).reset()  # what `lastcall password reset` does, with the store's sessions
    web.store.drop_sessions()
    assert web.client.get("/").headers["Location"] == "/setup"
    web.client.get("/setup")
    assert web.code() != first


def test_passwords_and_sessions() -> None:
    stored = hash_password(PASSWORD)
    assert check_password(stored, PASSWORD) and not check_password(stored, PASSWORD + " ")
    assert not check_password("garbage", PASSWORD)


def test_sessions_end(tmp_path: Path) -> None:
    store = Store(tmp_path / "lastcall.db")
    token = start_session(store, NOW)
    assert find_session(store, token, NOW + 3 * DAY) is not None  # used now: idle time starts again
    assert find_session(store, token, NOW + 3 * DAY + IDLE_DAYS * DAY + 1) is None
    token = start_session(store, NOW)
    for day in range(1, SESSION_DAYS):
        assert find_session(store, token, NOW + day * DAY) is not None
    assert find_session(store, token, NOW + SESSION_DAYS * DAY + 1) is None
    assert find_session(store, "not a token", NOW) is None


def test_many_failures_from_anywhere_lock_everyone() -> None:
    clock = Clock()
    throttle = Throttle(clock)
    for n in range(LOCK_ALL):
        throttle.fail(f"10.0.0.{n}")
    assert throttle.wait("10.0.1.1") == 15 * 60
    clock.now += 15 * 60
    assert throttle.wait("10.0.1.1") == 0


@pytest.fixture
def github_web(tmp_path: Path) -> Web:
    web = Web(tmp_path, github=FakeGitHub())
    web.set_up()
    return web


def test_projects_page_without_the_github_app(web: Web) -> None:
    web.set_up()
    page = web.client.get("/projects").get_data(as_text=True)
    assert "Create the GitHub App first" in page and 'aria-current="page">Projects' in page
    response = web.client.post("/projects/add", data={"csrf": web.csrf(), "repo": "acme/api"}, headers=SITE)
    assert response.status_code == 400


def test_add_a_project_by_its_address(github_web: Web) -> None:
    web = github_web
    csrf = web.csrf()

    def add(repo: str) -> tuple[int, str]:
        response = web.client.post("/projects/add", data={"csrf": csrf, "repo": repo}, headers=SITE)
        return response.status_code, response.get_data(as_text=True)

    status, page = add("not a repository")
    assert status == 400 and "That is not a GitHub repository" in page
    status, page = add("https://github.com/acme/elsewhere")
    assert status == 400 and "not installed on acme/elsewhere" in page
    assert 'href="https://github.com/apps/lastcall-test/installations/new"' in page
    status, _ = add("https://github.com/ACME/API/pulls")
    assert status == 302
    assert [p.repo for p in web.store.projects()] == ["acme/api"]  # as GitHub writes it
    web.store.update_project("acme/api", active=True, weight=2, language="Spanish", max_prs=2)
    add("acme/api")  # adding it again keeps its settings
    assert web.store.projects()[0].weight == 2


def test_project_settings(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    form = {"csrf": web.csrf(), "weight": "2", "language": "Spanish", "max_prs": "1"}
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    project = web.store.projects()[0]
    assert (project.active, project.weight, project.language, project.max_prs) == (False, 2, "Spanish", 1)
    page = web.client.get("/projects").get_data(as_text=True)
    assert '<option value="2" selected>High (' + chr(0xD7) + "2)</option>" in page  # the multiplication sign
    assert '<option value="Spanish" selected>Spanish</option>' in page
    for wrong in ({"weight": "0"}, {"weight": "x"}, {"max_prs": "99"}, {"language": "Klingon"}):
        response = web.client.post("/projects/acme/api", data={**form, **wrong}, headers=SITE)
        assert response.status_code == 400
    assert web.client.post("/projects/acme/nothing", data=form, headers=SITE).status_code == 404


def test_audit_now_queues_one_order(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    page = web.client.get("/projects").get_data(as_text=True)
    assert "Add an AI account to audit it." in page
    web.store.add_account("claude", "claude-code")
    csrf = web.csrf()
    wrong = web.client.post("/projects/acme/api/audit", data={"csrf": csrf, "account": "nobody"}, headers=SITE)
    assert wrong.status_code == 400
    for _ in range(2):
        response = web.client.post("/projects/acme/api/audit", data={"csrf": csrf, "account": ""}, headers=SITE)
        assert response.headers["Location"] == "/projects?done=queued#project-acme--api"
    assert [(o["repo"], o["account"]) for o in web.store.orders()] == [("acme/api", None)]
    page = web.client.get("/projects?done=queued").get_data(as_text=True)
    assert "Audit queued. It starts within a minute" in page and "Audit queued: it starts as soon as" in page


def test_suggestions_in_spanish(github_web: Web) -> None:
    web = github_web
    page = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Aún no se ha leído de GitHub." in page
    response = web.client.post("/projects/refresh", data={"csrf": web.csrf()}, headers=SITE)
    assert response.headers["Location"] == "/projects?done=refreshing"
    page = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Leído de GitHub ahora mismo." in page
    assert ">acme/api</label>" in page and '</a> <span class="badge">sugerido</span>' in page
    assert "tests · CI · 1\u00a0issue abierto" in page and "unos 11k tokens de código" in page
    assert "<summary>1 repositorio más</summary>" in page and "excluido: archivado" in page
    web.store.add_project("acme/api")
    page = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "sugerido" not in page  # projects leave the suggestions


def test_the_dashboard_sends_to_the_projects_page(github_web: Web) -> None:
    page = github_web.client.get("/").get_data(as_text=True)
    assert 'Choose your projects: lastcall suggests them and says why. <a href="/projects">Projects</a>' in page


def test_notification_settings(web: Web) -> None:
    web.set_up()
    page = web.client.get("/settings").get_data(as_text=True)
    assert "No destinations yet" in page and 'aria-current="page">Settings' in page
    assert '<option value="telegram">Telegram</option>' in page and "Find my chat" in page
    assert re.search(r'name="topic" type="text" value="lastcall-[a-z0-9]{16}"', page)  # a random ntfy topic, ready
    assert "apprise/wiki" not in page
    csrf = web.csrf()

    def add(**form: str) -> Any:
        return web.client.post(
            "/settings/notify/add", data={"csrf": csrf, **form}, headers={**SITE, "Accept-Language": "es"}
        )

    bad = add(service="telegram", token="12345", chat="1")
    assert bad.status_code == 400 and "Telegram no acepta ese token" in bad.get_data(as_text=True)
    assert add(service="telegram", token=BOT, chat="").status_code == 400  # the chat is needed
    web.outbox.ok = False
    failed = add(service="telegram", token=BOT, chat="12345")
    page = failed.get_data(as_text=True)
    assert failed.status_code == 400 and "no se ha guardado" in page
    assert "There were not Telegram chat_ids to notify." in page  # what Apprise said
    assert '<option value="telegram" selected>' in page and f'value="{BOT}"' in page  # the form keeps its values
    assert web.secrets.get_json("notify") is None
    web.outbox.ok = True
    added = add(service="telegram", token=BOT, chat="12345")
    assert added.headers["Location"] == "/settings?done=notify_added#notify"
    assert web.outbox.sent[-1] == (
        [f"tgram://{BOT}/12345"],
        "lastcall: notificación de prueba",
        web.outbox.sent[-1][2],
    )
    assert web.store.setting("notify_language") == "es"  # the first destination takes the page's language
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Telegram: tgram://" in page and BOT not in page

    form = {"csrf": csrf, "events": ["pr", "burst"], "language": "es"}
    assert web.client.post("/settings/notify", data=form, headers=SITE).status_code == 302
    assert (web.store.setting("notify_events"), web.store.setting("notify_language")) == ("pr,burst", "es")
    assert web.client.post("/settings/notify", data={**form, "language": "xx"}, headers=SITE).status_code == 400
    sent = web.client.post("/settings/notify/test/0", data={"csrf": csrf}, headers=SITE)
    assert sent.headers["Location"] == "/settings?done=notify_sent#notify"
    web.outbox.ok = False
    failed = web.client.post("/settings/notify/test/0", data={"csrf": csrf}, headers=SITE)
    assert failed.status_code == 502 and "There were not Telegram chat_ids" in failed.get_data(as_text=True)
    assert web.client.post("/settings/notify/test/9", data={"csrf": csrf}, headers=SITE).status_code == 404

    web.client.post("/settings/notify/remove", data={"csrf": csrf, "index": "0"}, headers=SITE)
    assert web.secrets.get_json("notify") == {"urls": []}
    wrong = web.client.post("/settings/notify/remove", data={"csrf": csrf, "index": "x"}, headers=SITE)
    assert wrong.status_code == 400


def test_find_the_telegram_chat(tmp_path: Path) -> None:
    replies: list[tuple[int, bytes]] = []

    def fetch(request: Any) -> tuple[int, dict[str, str], bytes]:
        assert request.full_url == f"https://api.telegram.org/bot{BOT}/getUpdates"
        status, body = replies.pop(0)
        return status, {}, body

    web = Web(tmp_path, fetch=fetch)
    web.set_up()
    csrf = web.csrf()

    def find(token: str = BOT) -> str:
        response = web.client.post("/settings/notify/telegram", data={"csrf": csrf, "token": token}, headers=SITE)
        return response.get_data(as_text=True)

    replies.append((401, b'{"ok": false}'))
    assert "Telegram does not accept that token" in find()
    replies.append((200, b'{"ok": true, "result": []}'))
    assert "Your bot has no recent messages" in find()
    update = {"update_id": 1, "message": {"chat": {"id": 987654321, "type": "private", "first_name": "Ana"}}}
    replies.append((200, json.dumps({"ok": True, "result": [update]}).encode()))
    page = find()
    assert 'type="radio" name="chat" value="987654321" checked' in page and "Ana (private)" in page
    assert '<option value="telegram" selected>' in page and f'value="{BOT}"' in page


def test_create_the_github_app_from_the_web(tmp_path: Path) -> None:
    codes: list[str] = []

    def exchange(code: str) -> dict[str, Any]:
        codes.append(code)
        return {
            "id": 42,
            "slug": "lastcall-acme",
            "name": "lastcall-acme",
            "owner": {"login": "acme"},
            "html_url": "https://github.com/apps/lastcall-acme",
            "pem": "-----BEGIN RSA PRIVATE KEY-----",
        }

    web = Web(tmp_path, exchange=exchange)
    web.set_up()
    assert '<a href="/github">GitHub App</a>' in web.client.get("/").get_data(as_text=True)
    assert 'name="owner"' in web.client.get("/github").get_data(as_text=True)
    assert web.client.get("/github/new?owner=not%20a%20login").status_code == 400

    response = web.client.get("/github/new?owner=acme&org=1")
    page = response.get_data(as_text=True)
    assert "form-action 'self' https://github.com" in response.headers["Content-Security-Policy"]
    action = re.search(r'<form method="post" action="(https://github\.com[^"]+)"', page)
    assert action and action.group(1).startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    state = parse_qs(urlsplit(action.group(1).replace("&amp;", "&")).query)["state"][0]
    assert "http://localhost/github/callback" in page  # where GitHub brings the owner back
    assert "contents: write" in page
    assert web.client.get("/").headers["Content-Security-Policy"] == CSP  # only that page may post to GitHub

    assert web.client.get("/github/callback?code=abc&state=forged").status_code == 400
    done = web.client.get(f"/github/callback?code=abc&state={state}")
    assert done.headers["Location"] == "/github?done=app_created"
    app = App.load(web.secrets)
    assert app and (app.id, app.owner) == (42, "acme") and codes == ["abc"]
    assert web.client.get(f"/github/callback?code=abc&state={state}").status_code == 400  # a state works once
    page = web.client.get("/github?done=app_created").get_data(as_text=True)
    assert 'href="https://github.com/apps/lastcall-acme/installations/new"' in page


def test_accounts_page(tmp_path: Path) -> None:
    readings: list[Account] = []

    def read(account: Account) -> Reading:
        readings.append(account)
        if account.id == "broken":
            raise QuotaError("not signed in")
        return Reading(account.id, (Window("week", 12, NOW + 30 * HOUR, "expiring", 168.0),), False, "exact", NOW)

    web = Web(tmp_path, read_quota=read)
    web.set_up()
    csrf = web.csrf()
    assert '<a href="/accounts">Accounts</a>' in web.client.get("/").get_data(as_text=True)
    assert 'hreflang="es">Español</a>' not in web.client.get("/accounts").get_data(as_text=True)
    settings = web.client.get("/settings").get_data(as_text=True)
    assert 'hreflang="es">Español</a>' in settings  # the language is a setting

    def add(**form: str) -> tuple[int, str]:
        response = web.client.post("/accounts/add", data={"csrf": csrf, **form}, headers=SITE)
        return response.status_code, response.get_data(as_text=True)

    status, page = add(id="Not Valid", engine="claude-code", language="English", reserve="10")
    assert status == 400 and "lowercase letters, digits and dashes" in page
    assert add(id="max", engine="nothing", language="English", reserve="10")[0] == 400
    assert add(id="max", engine="claude-code", language="English", reserve="10")[0] == 302
    assert add(id="max", engine="codex", language="English", reserve="10")[0] == 400  # the name is taken
    assert add(id="broken", engine="codex", language="Spanish", reserve="20")[0] == 302
    assert [(a.id, a.engine, a.language, a.reserve_pct) for a in web.store.accounts()] == [
        ("broken", "codex", "Spanish", 20),
        ("max", "claude-code", "English", 10),
    ]
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "ACCOUNT=max ENGINE=claude-code docker compose run --rm login" in page
    assert '$env:ACCOUNT="broken"; $env:ENGINE="codex"; docker compose run --rm login' in html.unescape(page)
    assert '<option value="claude-code">Claude (Claude Code)</option>' in page  # engines name themselves

    for name in ("max", "broken"):
        response = web.client.post(f"/accounts/{name}/quota", data={"csrf": csrf}, headers=SITE)
        assert response.headers["Location"] == f"/accounts?done=reading#account-{name}"
    assert [a.id for a in readings] == ["max", "broken"]
    assert web.store.last_reading("max") is not None
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "The last reading failed: not signed in" in page and "12% used" in page

    form = {"csrf": csrf, "language": "Spanish", "reserve": "15"}
    saved = web.client.post("/accounts/max", data=form, headers=SITE)
    assert saved.status_code == 302 and saved.headers["Location"] == "/accounts?done=saved#account-max"
    row = next(a for a in web.store.accounts() if a.id == "max")
    assert (row.enabled, row.language, row.reserve_pct) == (False, "Spanish", 15)
    assert web.client.post("/accounts/max", data={**form, "reserve": "95"}, headers=SITE).status_code == 400
    assert web.client.post("/accounts/nobody", data=form, headers=SITE).status_code == 404


def test_accounts_on_an_api_key_and_on_a_local_model(web: Web) -> None:
    web.set_up()
    csrf = web.csrf()
    page = web.client.get("/accounts").get_data(as_text=True)
    assert '<option value="key">An API key, with a monthly budget</option>' in page
    assert '<a href="https://openrouter.ai/settings/keys">OpenRouter</a>' in page  # where to get a key
    assert "docker compose --profile local run --rm ollama-pull qwen3-coder" in page

    def add(**form: str) -> tuple[int, str]:
        response = web.client.post("/accounts/add-key", data={"csrf": csrf, **form}, headers=SITE)
        return response.status_code, response.get_data(as_text=True)

    key = {
        "kind": "key",
        "id": "openrouter",
        "source": "opencode/openrouter",
        "key": "sk-or-test",
        "model": "qwen/qwen3-coder",
        "budget": "20",
        "policy": "budget",
        "language": "English",
    }
    status, page = add(**{**key, "budget": ""})
    assert status == 400 and "An API key needs a monthly budget" in page
    assert 'value="qwen/qwen3-coder"' in page and "sk-or-test" not in page  # what was written stays, but the key
    assert '<option value="key" selected>' in page  # the same form opens again
    assert "Give the API key." in add(**{**key, "key": ""})[1]
    assert "Give the model" in add(**{**key, "model": ""})[1]
    assert "five cron fields" in add(**{**key, "policy": "schedule", "schedule": "nightly"})[1]
    assert add(**{**key, "policy": "before-reset"})[0] == 400  # only a forged form
    assert add(**{**key, "source": "claude-code/openrouter"})[0] == 400
    response = web.client.post("/accounts/add-key", data={"csrf": csrf, **key}, headers=SITE)
    assert response.headers["Location"] == "/accounts?done=key_added"
    assert "It needs no login" in web.client.get(response.headers["Location"]).get_data(as_text=True)
    assert web.secrets.get("key-openrouter") == "sk-or-test"
    row = web.store.account("openrouter")
    assert row is not None
    assert (row.provider, row.policy, row.budget_usd, row.models["balanced"]) == (
        "openrouter",
        "budget",
        20,
        "qwen/qwen3-coder",
    )
    assert "already an account" in add(**key)[1]

    local = {"kind": "local", "id": "local", "source": "opencode/ollama", "model": "qwen3-coder"}
    assert add(**local, policy="schedule", schedule="* 1-6 * * *", language="Spanish")[0] == 302
    assert web.secrets.get("key-local") is None

    web.store.record_spend("openrouter", 5.0, "judge", at=NOW)
    web.store.set_setting("heartbeat", str(NOW))  # the service runs: otherwise nothing bursts, and the card says so
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "On OpenRouter · model qwen/qwen3-coder · writes in English" in page
    assert "Budget this month" in page and "$5.00 of $20.00" in page
    assert "Burst in progress: $5.00 of $20.00 spent this month, behind the calendar." in page
    assert "Local models cost nothing." in page
    # a schedule never breaks across lines: its fields are joined by no-break spaces, its ranges by no-break hyphens
    assert "at the hours of its schedule (*\u00a01\u20116\u00a0*\u00a0*\u00a0*, UTC)" in page
    assert "ACCOUNT=openrouter" not in page  # an API key has no login

    form = {
        "csrf": csrf,
        "enabled": "1",
        "language": "English",
        "policy": "schedule",
        "schedule": "* 0-5 * * 1-5",
        "budget": "30",
        "key": "sk-or-new",
    }
    models = web.store.account("openrouter")
    assert web.client.post("/accounts/openrouter", data=form, headers=SITE).status_code == 302
    row = web.store.account("openrouter")
    assert row is not None and (row.policy, row.schedule, row.budget_usd) == ("schedule", "* 0-5 * * 1-5", 30)
    assert models is not None and row.models == models.models  # they change under Models and effort
    # a schedule copied from a plan sentence (no-break hyphen and spaces) is accepted and stored as plain text
    pasted = {**form, "schedule": "*\u00a01\u20116\u00a0*\u00a0*\u00a0*", "key": ""}
    assert web.client.post("/accounts/openrouter", data=pasted, headers=SITE).status_code == 302
    row = web.store.account("openrouter")
    assert row is not None and row.schedule == "* 1-6 * * *"
    assert web.client.post("/accounts/openrouter", data=form, headers=SITE).status_code == 302
    assert web.secrets.get("key-openrouter") == "sk-or-new"
    assert web.client.post("/accounts/openrouter", data={**form, "key": ""}, headers=SITE).status_code == 302
    assert web.secrets.get("key-openrouter") == "sk-or-new"  # an empty field keeps the key
    assert web.client.post("/accounts/local", data={**form, "policy": "budget"}, headers=SITE).status_code == 400

    web.client.get("/language/es")
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "Una clave de API, con un presupuesto mensual" in page and "5,00 USD de 30,00 USD" in page


def test_models_and_effort_from_the_web(web: Web) -> None:
    from lastcall.accounts import tier_models

    web.set_up()
    web.store.add_account("max", "claude-code")
    web.store.add_account("local", "opencode", provider="ollama", models=tier_models("qwen3-coder"), policy="manual")
    csrf = web.csrf()
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "Models and effort" in page and 'placeholder="opus"' in page and "Default (medium)" in page
    form = {"csrf": csrf, **{f"{field}-{tier}": "" for field in ("model", "effort") for tier in TIERS}}
    response = web.client.post("/accounts/max/tiers", data={**form, "effort-deep": "max"}, headers=SITE)
    assert response.headers["Location"] == "/accounts?done=tiers#account-max"
    row = web.store.account("max")
    assert row is not None and row.account().tiers()["deep"] == ("opus", "max")
    assert '<option value="max" selected>max</option>' in web.client.get("/accounts").get_data(as_text=True)
    web.client.post("/accounts/max/tiers", data={**form, "model-balanced": "claude-sonnet-5-5"}, headers=SITE)
    row = web.store.account("max")  # the effort of deep went back to the default: its field was empty
    assert row is not None and row.account().tiers() == {
        "fast": ("haiku", "medium"),
        "balanced": ("claude-sonnet-5-5", "high"),
        "deep": ("opus", "high"),
    }
    for bad in ({"effort-deep": "ultra"}, {"model-deep": "opus; rm -rf /"}):
        assert web.client.post("/accounts/max/tiers", data={**form, **bad}, headers=SITE).status_code == 400
    assert web.client.post("/accounts/nobody/tiers", data=form, headers=SITE).status_code == 404
    local = {**form, "model-fast": "qwen3-coder", "model-balanced": "qwen3-coder", "effort-deep": "high"}
    response = web.client.post("/accounts/local/tiers", data=local, headers=SITE)  # no model for deep
    assert response.status_code == 400 and "Give a model for each level" in response.get_data(as_text=True)
    web.client.post("/accounts/local/tiers", data={**local, "model-deep": "qwen3-coder:30b"}, headers=SITE)
    row = web.store.account("local")
    assert row is not None and row.account().tiers()["deep"] == ("qwen3-coder:30b", "high")


def test_without_the_sandbox_the_quota_cannot_be_read(web: Web) -> None:
    web.set_up()
    web.store.add_account("max", "claude-code")
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "Read the quota now" not in page and "help-accounts-check" not in page  # no button, no bubble for it
    response = web.client.post("/accounts/max/quota", data={"csrf": web.csrf()}, headers=SITE)
    assert response.status_code == 503 and "The sandbox is not available here" in response.get_data(as_text=True)


def test_fan_out_from_the_web(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_account("claude", "claude-code")
    page = web.client.get("/projects").get_data(as_text=True)
    assert "All free accounts, one judge" not in page  # one account: nothing to fan out
    web.store.add_account("chatgpt", "codex")
    page = web.client.get("/projects").get_data(as_text=True)
    assert '<option value="*">All free accounts, one judge</option>' in page
    csrf = web.csrf()
    web.client.post("/projects/acme/api/audit", data={"csrf": csrf, "account": "*"}, headers=SITE)
    assert [o["account"] for o in web.store.orders()] == ["*"]

    page = web.client.get("/settings").get_data(as_text=True)
    assert 'aria-describedby="fanout-hint"> When several accounts burst' in page
    assert "two that reset on different days rarely meet" in page
    web.client.post("/settings/runs", data={"csrf": csrf, "fanout": "1"}, headers=SITE)
    assert web.store.setting("fanout") == "yes"
    assert 'checked aria-describedby="fanout-hint">' in web.client.get("/settings").get_data(as_text=True)
    web.client.post("/settings/runs", data={"csrf": csrf}, headers=SITE)
    assert web.store.setting("fanout") == "no"

    web.store.start_run("r1", "acme/api", "claude", "manual", ["chatgpt"])
    page = web.client.get("/").get_data(as_text=True)
    assert "Working on acme/api, started" in page  # the run's start comes from the real clock
    assert "Reviewing acme/api with claude, started" in page


def test_the_dashboard_shows_the_window_of_a_model(web: Web) -> None:
    web.store.add_account("claude", "claude-code", "Spanish", 10)
    week = Window("week", 30, NOW + 30 * HOUR, "expiring", 168.0)
    opus = Window("week", 93, NOW + 30 * HOUR, "expiring", 168.0, "Opus")
    web.store.record_reading(Reading("claude", (week, opus), False, "exact", NOW - 600))
    web.set_up()
    page = web.client.get("/", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert "Weekly window for Opus" in page
    assert "No burst: the audit needs Opus, and its own window is at 93%. The rest is your reserve." in page
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Ventana semanal de Opus" in page
    assert "Sin ráfaga: la auditoría necesita Opus, y su propia ventana está al 93\u00a0%." in page


def test_improve_now(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_account("claude", "claude-code")
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'formaction="/projects/acme/api/improve">Improve now</button>' in page
    assert "Improve now: lastcall proposes small improvements in an issue with checkboxes" in page
    csrf = web.csrf()
    wrong = web.client.post("/projects/acme/api/improve", data={"csrf": csrf, "account": "nobody"}, headers=SITE)
    assert wrong.status_code == 400
    response = web.client.post("/projects/acme/api/improve", data={"csrf": csrf, "account": "*"}, headers=SITE)
    assert response.headers["Location"] == "/projects?done=improve#project-acme--api"
    assert [(o["kind"], o["repo"], o["account"]) for o in web.store.orders()] == [("improve", "acme/api", None)]
    page = web.client.get("/projects?done=improve").get_data(as_text=True)
    assert "Improvement queued. It starts within a minute" in page and "Improvement queued: it starts" in page
    web.store.start_order(web.store.orders()[0]["id"], NOW)
    web.store.start_run("r1", "acme/api", "claude", "manual", recipe="improve")
    assert "lastcall is improving this project." in web.client.get("/projects").get_data(as_text=True)


def test_tests_from_the_web(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_account("claude", "claude-code")
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'formaction="/projects/acme/api/tests">Write tests now</button>' in page
    assert 'name="tests" value="1"> Tests too</label>' in page
    csrf = web.csrf()
    form = {"csrf": csrf, "weight": "1", "language": "", "max_prs": "3", "active": "1", "tests": "1"}
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    assert web.store.projects()[0].tests
    response = web.client.post("/projects/acme/api/tests", data={"csrf": csrf, "account": "claude"}, headers=SITE)
    assert response.headers["Location"] == "/projects?done=tests#project-acme--api"
    assert [(o["kind"], o["repo"], o["account"]) for o in web.store.orders()] == [("tests", "acme/api", "claude")]
    page = web.client.get("/projects?done=tests").get_data(as_text=True)
    assert "Tests queued. They start within a minute" in page and "Tests queued: they start" in page
    web.store.start_order(web.store.orders()[0]["id"], NOW)
    web.store.start_run("r1", "acme/api", "claude", "manual", recipe="tests")
    assert "lastcall is writing tests for this project." in web.client.get("/projects").get_data(as_text=True)
    assert "manual · tests" in web.client.get("/").get_data(as_text=True)


def test_dependency_upgrades_from_the_web(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_account("claude", "claude-code")
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'formaction="/projects/acme/api/deps">Upgrade dependencies now</button>' in page
    csrf = web.csrf()
    form = {"csrf": csrf, "weight": "1", "language": "", "max_prs": "3", "active": "1", "deps": "1"}
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    project = web.store.projects()[0]
    assert project.deps and not project.tests
    assert 'name="deps" value="1" checked> Dependencies too' in web.client.get("/projects").get_data(as_text=True)
    response = web.client.post("/projects/acme/api/deps", data={"csrf": csrf}, headers=SITE)
    assert response.headers["Location"] == "/projects?done=deps#project-acme--api"
    assert [(o["kind"], o["account"]) for o in web.store.orders()] == [("deps", None)]
    assert "Upgrades queued: they start" in web.client.get("/projects?done=deps").get_data(as_text=True)


def test_errors_in_production_from_the_web(tmp_path: Path) -> None:
    from lastcall import signals

    asked: list[str] = []

    def tracker(url: str, headers: Mapping[str, str]) -> Any:
        asked.append(url)
        if headers["Authorization"] != "Bearer good-token":
            raise signals.SignalsError("the token is not valid: https://glitchtip.example.com/api/0/organizations/")
        return []

    web = Web(tmp_path, signals_fetch=tracker)
    web.set_up()
    csrf = web.csrf()
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Errors in production" in page
    assert 'href="https://sentry.io/settings/account/api/auth-tokens/"' in page  # where to create a token

    def connect(**form: str) -> Any:
        return web.client.post("/settings/signals", data={"csrf": csrf, **form}, headers=SITE)

    form = {"url": "https://glitchtip.example.com/", "org": "acme", "token": "bad"}
    response = connect(**form)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and "The tracker did not answer as expected: the token is not valid" in page
    assert 'value="https://glitchtip.example.com"' in page and 'value="acme"' in page  # what was written stays
    assert web.secrets.get("signals") is None  # saved only when it works
    assert connect(**{**form, "url": "ftp://glitchtip.example.com"}).status_code == 400
    assert connect(**{**form, "org": "a/b"}).status_code == 400
    response = connect(**{**form, "token": "good-token"})
    assert response.headers["Location"] == "/settings?done=signals#signals"
    assert asked[-1] == "https://glitchtip.example.com/api/0/organizations/acme/issues/?limit=1"
    page = web.client.get("/settings?done=signals").get_data(as_text=True)
    assert "Connected to https://glitchtip.example.com, organization acme." in page and "good-token" not in page
    assert connect(url="https://glitchtip.example.com", org="acme", token="").status_code == 302  # keeps the token
    assert signals.Tracker.load(web.secrets) == signals.Tracker("https://glitchtip.example.com", "acme", "good-token")

    web.store.add_project("acme/shop")
    assert 'name="signals"' in web.client.get("/projects").get_data(as_text=True)
    settings = {"csrf": csrf, "weight": "1", "language": "", "max_prs": "3", "active": "1"}
    assert (
        web.client.post("/projects/acme/shop", data={**settings, "signals": "shop"}, headers=SITE).status_code == 302
    )
    assert web.store.projects()[0].signals == "shop"
    assert web.client.post("/projects/acme/shop", data=settings, headers=SITE).status_code == 302
    assert web.store.projects()[0].signals == "shop"  # a form without the field keeps it
    assert (
        web.client.post("/projects/acme/shop", data={**settings, "signals": "a b"}, headers=SITE).status_code == 400
    )

    response = web.client.post("/settings/signals/remove", data={"csrf": csrf}, headers=SITE)
    assert response.headers["Location"] == "/settings?done=signals_removed#signals"
    assert web.secrets.get("signals") is None
    assert 'name="signals"' not in web.client.get("/projects").get_data(as_text=True)


def test_a_token_with_a_line_break_or_an_address_with_a_space_never_reaches_the_tracker(tmp_path: Path) -> None:
    def unexpected(*args: object) -> Any:
        raise AssertionError("nothing is sent")

    web = Web(tmp_path, signals_fetch=unexpected, loki_fetch=unexpected)
    web.set_up()
    csrf = web.csrf()
    form = {
        "csrf": csrf,
        "url": "https://glitchtip.example.com",
        "org": "acme",
        "token": "sntrys_SECRET" + chr(10) + "TAIL",
    }
    response = web.client.post("/settings/signals", data=form, headers=SITE)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and "line breaks" in page and "sntrys_SECRET" not in page
    assert web.secrets.get("signals") is None
    for path, fields in (
        ("/settings/signals", {"org": "acme", "token": "tok"}),
        ("/settings/loki", {"user": "", "tenant": "", "token": "tok"}),
    ):
        for url in ("http://127.0.0.1:9/a b", "http://127.0.0.1:9/a" + chr(10) + "b"):
            response = web.client.post(path, data={"csrf": csrf, "url": url, **fields}, headers=SITE)
            assert response.status_code == 400, (path, url)


def test_container_logs_from_the_web(tmp_path: Path) -> None:
    from lastcall import logs
    from lastcall.sandbox import SandboxError

    listed = [
        {"name": "shop-web-1", "image": "ghcr.io/acme/shop:1.2", "state": "running", "compose_project": "shop"},
        {"name": "blog", "image": "nginx:1.27", "state": "running", "label": "acme/blog"},
        {"name": "redis", "image": "redis:7", "state": "running"},
    ]
    web = Web(tmp_path, containers=lambda: listed)
    web.set_up()
    web.store.add_project("acme/shop")
    web.store.add_project("acme/blog")
    csrf = web.csrf()
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Container logs" in page and "Suggested: acme/shop. Save to read it." in page
    assert '<option value="acme/shop" selected>acme/shop</option>' in page  # the suggestion comes chosen
    assert "Read for acme/blog, from its label." in page and "lastcall.project: owner/name" in page
    assert '<input type="hidden" name="shown-1" value="acme/shop">' in page
    # each source's select says which container it is for, not only "Project"
    assert (
        '<strong id="logs-name-1">shop-web-1</strong>' in page
        and 'id="logs-1" aria-describedby="logs-name-1"' in page
    )
    form = {"csrf": csrf, "name-1": "shop-web-1", "project-1": "acme/shop", "shown-1": "acme/shop"}
    form |= {"name-3": "redis", "project-3": "", "shown-3": ""}
    response = web.client.post("/settings/logs", data=form, headers=SITE)
    assert response.headers["Location"] == "/settings?done=logs#logs"
    assert logs.chosen(web.store) == {"shop-web-1": "acme/shop"}  # redis: no project, and none was shown
    bad = {**form, "project-3": "someone/else"}
    assert web.client.post("/settings/logs", data=bad, headers=SITE).status_code == 400
    web.client.post("/settings/logs", data={**form, "project-1": ""}, headers=SITE)  # the owner says no to it
    assert logs.chosen(web.store) == {"shop-web-1": ""}
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Suggested:" not in page and '<option value="acme/shop" selected>' not in page  # not suggested again

    def broken() -> list[dict[str, Any]]:
        raise SandboxError("supervisor unreachable")

    alone = Web(tmp_path / "alone")  # `lastcall web` with no sandbox
    alone.set_up()
    assert "only when lastcall runs with docker compose" in alone.client.get("/settings").get_data(as_text=True)
    failing = Web(tmp_path / "failing", containers=broken)
    failing.set_up()
    page = failing.client.get("/settings").get_data(as_text=True)
    assert "The containers could not be listed: supervisor unreachable" in page

    def unexpected() -> list[dict[str, Any]]:
        raise AttributeError("module 'socket' has no attribute 'AF_UNIX'")  # lastcall web on Windows

    odd = Web(tmp_path / "odd", containers=unexpected)
    odd.set_up()
    response = odd.client.get("/settings")  # an optional integration never takes the page down
    assert response.status_code == 200 and "The containers could not be listed:" in response.get_data(as_text=True)


def test_loki_from_the_web(tmp_path: Path) -> None:
    from lastcall import loki

    series = [{"container": "shop-web-1", "compose_project": "shop", "host": "vps-1"}, {"container": "redis"}]
    fake = FakeLoki(series)

    def fetch(url: str, headers: Mapping[str, str]) -> Any:
        if url.startswith("http://down.example.com"):
            raise loki.LokiError("no answer from http://down.example.com:3100")
        return fake(url, headers)

    web = Web(tmp_path, loki_fetch=fetch)
    web.set_up()
    web.store.add_project("acme/shop")
    csrf = web.csrf()
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Logs in Loki" in page and "Connected to" not in page
    form = {"csrf": csrf, "url": "http://down.example.com:3100", "user": "", "token": "", "tenant": ""}
    response = web.client.post("/settings/loki", data=form, headers=SITE)
    assert response.status_code == 400 and loki.Loki.load(web.secrets) is None
    assert "Loki could not be read: no answer from http://down.example.com:3100" in response.get_data(as_text=True)
    form["url"] = "http://loki.example.com:3100/"
    response = web.client.post("/settings/loki", data=form, headers=SITE)
    assert response.headers["Location"] == "/settings?done=loki#loki"
    assert loki.Loki.load(web.secrets) == loki.Loki("http://loki.example.com:3100")
    login = {**form, "user": "123456", "token": "glc_t0ken"}
    web.client.post("/settings/loki", data=login, headers=SITE)
    web.client.post("/settings/loki", data={**login, "token": "", "tenant": "team-a"}, headers=SITE)  # the same login
    assert loki.Loki.load(web.secrets) == loki.Loki("http://loki.example.com:3100", "123456", "glc_t0ken", "team-a")
    page = web.client.get("/settings").get_data(as_text=True)
    assert "Connected to http://loki.example.com:3100." in page
    assert '<option value="container" selected>container</option>' in page
    assert "Compose project: shop" in page and "Suggested: acme/shop. Save to read it." in page
    assert 'id="loki-1" aria-describedby="loki-name-1"' in page and 'id="loki-name-1"' in page
    rows = {"csrf": csrf, "label": "container", "name-1": "redis", "project-1": "", "shown-1": ""}
    rows |= {"name-2": "shop-web-1", "project-2": "acme/shop", "shown-2": "acme/shop"}
    response = web.client.post("/settings/loki/sources", data=rows, headers=SITE)
    assert response.headers["Location"] == "/settings?done=loki_sources#loki"
    assert (web.store.setting(loki.LABEL), loki.chosen(web.store)) == ("container", {"shop-web-1": "acme/shop"})
    for bad in ({"name-1": "lastcall-step-abc123"}, {"project-1": "someone/else"}, {"label": "a b"}):
        assert web.client.post("/settings/loki/sources", data={**rows, **bad}, headers=SITE).status_code == 400
    response = web.client.post("/settings/loki/label", data={"csrf": csrf, "label": "host"}, headers=SITE)
    assert response.headers["Location"] == "/settings?done=loki_label#loki"
    assert (web.store.setting(loki.LABEL), loki.chosen(web.store)) == ("host", {})  # the choices start again
    web.client.post("/settings/loki/remove", data={"csrf": csrf}, headers=SITE)
    assert loki.Loki.load(web.secrets) is None


def test_the_dashboard_lists_plans_and_improvement_runs(web: Web) -> None:
    from lastcall.recipes.improve import ImproveReport

    web.set_up()
    plan = ImproveReport("ne0x86/demo", "abc", "claude-code", "English")
    plan.ideas = [{"id": f"I{n}", "title": "A", "kind": "feature", "change": "c"} for n in (1, 2)]
    plan.verdicts = [{"id": "I1", "verdict": "keep"}, {"id": "I2", "verdict": "keep"}]
    plan.plan, plan.issue_url = ["I1", "I2"], "https://github.com/ne0x86/demo/issues/3"
    web.store.record_ideas("r1", plan)
    web.store.start_run("r1", "ne0x86/demo", "claude", "manual", recipe="improve")
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Ideas para elegir" in page
    assert '<a href="https://github.com/ne0x86/demo/issues/3">2 ideas para ne0x86/demo</a>' in page
    assert "manual · mejora" in page


def test_merge_on_approve_and_watch_settings(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    form = {"csrf": web.csrf(), "weight": "1", "language": "", "max_prs": "3", "active": "1"}
    settings = {**form, "merge_on_approve": "1", "watch": "1", "health_url": " https://api.acme.example/health "}
    assert web.client.post("/projects/acme/api", data=settings, headers=SITE).status_code == 302
    project = web.store.projects()[0]
    assert (project.merge_on_approve, project.watch, project.health_url) == (
        True,
        True,
        "https://api.acme.example/health",
    )
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'name="merge_on_approve" value="1" checked> Merge after my approval' in page
    assert 'value="https://api.acme.example/health"' in page
    for wrong in ("ftp://acme.example/health", "javascript:alert(1)", "https://", "https://a b.example/"):
        response = web.client.post("/projects/acme/api", data={**form, "health_url": wrong}, headers=SITE)
        assert response.status_code == 400
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    project = web.store.projects()[0]
    assert (project.merge_on_approve, project.watch, project.health_url) == (False, False, None)


def test_a_project_says_how_many_labelled_issues_wait(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    found = [{"number": n, "url": f"https://github.com/acme/api/issues/{n}", "title": "t"} for n in (3, 4)]
    web.store.sync_tickets("acme/api", found)
    assert "2 labelled issues wait for a burst" in web.client.get("/projects").get_data(as_text=True)
    page = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "2 issues etiquetados esperan una ráfaga" in page


def two_accounts(web: Web) -> None:
    for name in ("claude", "max"):
        web.store.add_account(name, "claude-code", "English", 10)
        week = Window("week", 62, NOW + 30 * HOUR, "expiring", 168.0)
        web.store.record_reading(
            Reading(name, (Window("5h", 40, NOW + 2 * HOUR, "throttle", 5.0), week), False, "exact", NOW - 600)
        )


def test_a_help_button_shows_once_per_page_and_opens_its_popover(web: Web) -> None:
    two_accounts(web)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        buttons = re.findall(r'<button type="button" class="help" popovertarget="(help-[^"]+)"', page)
        assert buttons
        if path == "/":
            assert len(buttons) == len(set(buttons))  # one "?" per bubble, unless a form shows alone
        for target in buttons:
            assert f'id="{target}" popover' in page  # every "?" opens a popover that exists
        popovers = re.findall(r'id="(help-[^"]+)" popover', page)
        assert sorted(popovers) == sorted(set(buttons))  # and a popover exists only when one of its "?" does
    page = web.client.get("/accounts").get_data(as_text=True)
    assert page.count("<article") == 2
    for common in ("help-common-engine", "help-common-window_throttle", "help-common-window_expiring"):
        # two cards, one "?" for each: the second card does not repeat a common bubble
        assert page.count(f'class="help" popovertarget="{common}"') == 1
        assert page.count(f'id="{common}" popover') == 1


def test_no_card_says_bursting_while_the_service_is_down_or_paused(web: Web) -> None:
    two_accounts(web)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert "On hold" not in page and "Waiting for its moment" in page
    web.store.set_setting("paused", "yes")
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert page.count(">On hold</span>") == 2 and "Waiting for its moment" not in page
        assert 'class="plan is-held"' in page
        assert (
            "Burst in progress" not in page
            and page.count("Bursts are paused: this account spends no leftover quota on its own") == 2
        )
    web.store.set_setting("paused", "no")
    web.store.set_setting("heartbeat", str(NOW - 3 * HOUR))  # the service is gone
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert page.count(">On hold</span>") == 2 and "Bursting" not in page
        assert "Burst in progress" not in page and page.count("The service has stopped: nothing bursts") == 2


def test_a_run_that_goes_on_while_paused_is_work_not_a_burst(web: Web) -> None:
    two_accounts(web)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    web.store.set_setting("paused", "yes")
    paused = "Bursts are paused: this account spends no leftover quota on its own"
    for trigger in ("manual", "burst"):  # a burst that began before the pause goes on, and so does an order
        web.store.start_run(f"r-{trigger}", "acme/api", "claude", trigger)
        for path in ("/", "/accounts"):
            page = web.client.get(path).get_data(as_text=True)
            assert "Burst in progress" not in page and ">Bursting</span>" not in page
            assert ">Working</span>" in page and "Working on acme/api" in page
            assert page.count("the run under way finishes") == 1, trigger  # the working card
            assert page.count(paused) == 1, trigger  # the idle one
        web.store.finish_run(f"r-{trigger}", "ok", None)
    page = web.client.get("/accounts", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "la ejecución en curso termina" not in page  # nothing runs now
    web.store.start_run("r2", "acme/api", "claude", "burst")
    page = web.client.get("/accounts", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert ">Trabajando</span>" in page and "En ráfaga" not in page
    assert page.count("la ejecución en curso termina") == 1


def test_a_key_account_without_its_key_asks_for_a_key_not_a_sign_in(web: Web) -> None:
    from lastcall.accounts import tier_models

    web.store.add_account(
        "zen", "opencode", provider="openrouter", models=tier_models("qwen3-coder"), policy="budget", budget_usd=20
    )
    web.store.set_setting("heartbeat", str(NOW))
    web.set_up()
    page = web.client.get("/accounts").get_data(as_text=True)
    assert ">Needs a key</span>" in page and "Needs sign-in" not in page
    web.client.get("/language/es")
    assert ">Falta la clave</span>" in web.client.get("/accounts").get_data(as_text=True)


def test_every_field_of_the_add_forms_has_its_help(web: Web) -> None:
    web.store.add_account("claude", "claude-code", "English", 10)  # a card above them must not take their bubbles
    web.set_up()
    page = web.client.get("/accounts").get_data(as_text=True)
    forms = {
        "dest-subscription": ("id", "language"),
        "dest-key": ("key-id", "key-language"),
        "dest-local": ("local-id", "local-language"),
    }
    for form, fields in forms.items():
        section = page.split(f'<div class="dest {form}">')[1].split('<div class="dest ')[0]
        for field in fields:
            assert re.search(
                rf'<label for="{field}">[^<]*</label><button type="button" class="help" popovertarget="help-', section
            ), field
    # the fields of the card's own folds reach their help too
    card = page.split('id="account-claude"')[1].split("</article>")[0]
    for field in ("language-1", "reserve-1"):
        assert re.search(rf'<label for="{field}">[^<]*</label><button type="button" class="help"', card), field
    targets = re.findall(r'popovertarget="(help-[^"]+)"(?! popovertargetaction)', page)
    for target in set(targets):
        assert page.count(f'id="{target}" popover') == 1, target


def help_buttons_inside_a_paragraph(page: str) -> list[str]:
    from html.parser import HTMLParser

    found: list[str] = []

    class Walk(HTMLParser):
        depth = 0

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "p":
                self.depth += 1
            elif tag == "button" and self.depth and "help" in (dict(attrs).get("class") or "").split():
                found.append(dict(attrs).get("popovertarget") or "")

        def handle_endtag(self, tag: str) -> None:
            if tag == "p" and self.depth:
                self.depth -= 1

    Walk().feed(page)
    return found


def test_a_help_button_never_sits_inside_a_paragraph(github_web: Web) -> None:
    web = github_web
    two_accounts(web)
    web.store.add_project("acme/api")
    web.store.set_setting("heartbeat", str(NOW))
    pages = [web.client.get(path).get_data(as_text=True) for path in ("/", "/accounts", "/projects", "/settings")]
    refused = web.client.post("/projects/add", data={"csrf": web.csrf(), "repo": "acme/elsewhere"}, headers=SITE)
    assert "installations/new" in refused.get_data(as_text=True)
    pages.append(refused.get_data(as_text=True))
    for page in pages:
        assert 'class="help"' in page
        assert help_buttons_inside_a_paragraph(page) == []


def test_the_help_of_a_page_sits_next_to_what_it_explains(github_web: Web) -> None:
    web = github_web
    web.store.add_account("claude", "claude-code", "English", 10)
    page = web.client.get("/accounts").get_data(as_text=True)
    # the legend is the fieldset's first child, or the group has no name; the "?" comes after it
    groups = re.findall(
        r'<fieldset class="has-help">\s*<legend>When it spends</legend><button type="button" class="help"', page
    )
    assert len(groups) == 2
    # every long command can be scrolled from the keyboard
    assert "<pre>" not in page and page.count('<pre tabindex="0"') >= 4
    settings = web.client.get("/settings").get_data(as_text=True)
    assert "<pre>" not in settings and '<pre tabindex="0"' in settings


def test_every_destination_of_the_picker_has_the_help_of_its_test_button(web: Web) -> None:
    web.set_up()
    page = web.client.get("/settings").get_data(as_text=True)
    panels = page.split('<div class="dest dest-')[1:]
    assert len(panels) == 6
    for panel in panels:
        assert 'popovertarget="help-settings-save_test"' in panel.split('<div class="form-actions">')[-1], panel[:20]
    assert page.count('id="help-settings-save_test" popover') == 1


def test_a_cards_plan_keeps_its_help_when_an_earlier_card_repeated_it_in_a_fold(web: Web) -> None:
    web.store.add_account("api", "opencode", provider="openrouter", policy="budget", budget_usd=20)
    web.store.add_account("box", "opencode", provider="ollama", policy="schedule", schedule="* 1-6 * * *")
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    page = web.client.get("/accounts").get_data(as_text=True)
    box = page.split('id="account-box"')[1].split("</article>")[0]
    plan = box.split('<div class="plan')[1].split("</div>")[0]
    assert 'popovertarget="help-common-schedule"' in plan
    assert page.count('id="help-common-schedule" popover') == 1


def test_the_help_of_a_models_own_window_is_titled_for_it(web: Web) -> None:
    web.store.add_account("claude", "claude-code", "Spanish", 10)
    week = Window("week", 30, NOW + 30 * HOUR, "expiring", 168.0)
    opus = Window("week", 93, NOW + 30 * HOUR, "expiring", 168.0, "Opus")
    web.store.record_reading(Reading("claude", (week, opus), False, "exact", NOW - 600))
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    page = web.client.get("/", headers={"Accept-Language": "en"}).get_data(as_text=True)
    assert (
        "Help: A model&#39;s own window" in page
        and 'id="help-common-window_model-title">A model&#39;s own window' in page
    )
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Ayuda: La ventana propia de un modelo" in page


def test_the_two_tables_of_the_dashboard_name_their_own_help(web: Web) -> None:
    seed(web.store)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    page = web.client.get("/").get_data(as_text=True)
    assert (
        'id="help-dashboard-projects-title">Projects<' in page
        and 'id="help-dashboard-runs-title">Recent runs<' in page
    )


def test_the_readout_counts_what_waits_and_says_one_in_the_singular(web: Web) -> None:
    from lastcall.recipes.improve import ImproveReport

    seed(web.store)
    plan = ImproveReport("ne0x86/demo", "abc", "claude-code", "English")
    plan.ideas = [{"id": f"I{n}", "title": "A", "kind": "feature", "change": "c"} for n in (1, 2)]
    plan.verdicts = [{"id": "I1", "verdict": "keep"}, {"id": "I2", "verdict": "keep"}]
    plan.plan, plan.issue_url = ["I1", "I2"], "https://github.com/ne0x86/demo/issues/3"
    web.store.record_ideas("r1", plan)
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    readout = r'<span class="readout-n">(\d+)</span> <span class="readout-l">([^<]+)</span>'
    page = web.client.get("/").get_data(as_text=True)
    assert re.findall(readout, page) == [("1", "account"), ("4", "waiting for you"), ("1", "project")]
    # one pull request, one finding for a decision and two ideas: what the lists below name
    assert (
        "1 decision in ne0x86/demo" in page
        and "2 ideas for ne0x86/demo" in page
        and "Fix &lt;b&gt;it&lt;/b&gt;" in page
    )
    page = web.client.get("/", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert re.findall(readout, page) == [("1", "cuenta"), ("4", "pendientes de ti"), ("1", "proyecto")]


def test_the_projects_page_gives_each_project_a_card_with_its_help(github_web: Web) -> None:
    from lastcall import signals

    web = github_web
    web.store.add_project("acme/api")
    web.store.add_project("acme/web")
    web.store.update_project("acme/web", active=False, weight=1, language=None, max_prs=3)
    web.store.add_account("claude", "claude-code")
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'id="project-acme--api"' in page and 'id="project-acme--web"' in page
    assert (
        page.count('<article class="card project') == 2
        and 'class="card project is-off" id="project-acme--web"' in page
    )
    assert "<legend>Run now</legend>" in page and "<summary>Settings</summary>" in page
    assert "All free accounts, one judge" not in page and "Errors in production" not in page
    assert (
        "all free accounts" not in page and 'name="signals"' not in page
    )  # one account, no tracker: no such bubbles
    for field in (
        "weight-1",
        "language-1",
        "max-prs-1",
        "health-1",
        "weight-2",
        "language-2",
        "max-prs-2",
        "health-2",
    ):
        assert re.search(rf'<label for="{field}">[^<]*</label><button type="button" class="help"', page), field
    for name in ("merge_on_approve", "watch", "tests", "deps"):
        assert f'name="{name}" value="1"> ' in page
    targets = set(re.findall(r'popovertarget="(help-[^"]+)"(?! popovertargetaction)', page))
    assert targets
    for target in targets:
        assert page.count(f'id="{target}" popover') == 1, target
    assert sorted(re.findall(r'id="(help-[^"]+)" popover', page)) == sorted(targets)
    assert page.count('class="help" popovertarget="help-projects-account"') == 1  # a card's summary part: once
    assert page.count('class="help" popovertarget="help-projects-weight"') == 2  # a field in each closed fold
    web.store.add_account("chatgpt", "codex")
    signals.Tracker("https://glitchtip.example.com", "acme", "good-token").save(web.secrets)
    page = web.client.get("/projects").get_data(as_text=True)
    assert "With the option for all free accounts" in page  # two accounts: the choice exists, and so does its help
    assert 'name="signals"' in page and page.count("<legend>Errors in production</legend>") == 2
    assert "help-projects-signals" in page


def test_a_project_without_a_run_form_has_no_run_bubbles_and_an_empty_page_starts_with_adding(
    github_web: Web,
) -> None:
    web = github_web
    page = web.client.get("/projects").get_data(as_text=True)
    assert "No projects yet. Pick one of your repositories below" in page
    assert page.index("No projects yet.") < page.index('id="add-title"') and 'href="#add-title"' not in page
    web.store.add_project("acme/api")
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'href="#add-title">Add a project</a>' in page
    assert page.index('id="project-acme--api"') < page.index('id="add-title"')
    assert "help-projects-run_now" not in page and "Add an AI account to audit it." in page  # no account: no Run now


def test_saving_a_project_returns_to_its_card(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    form = {"csrf": web.csrf(), "weight": "1", "language": "", "max_prs": "3"}
    response = web.client.post("/projects/acme/api", data=form, headers=SITE)
    assert response.headers["Location"] == "/projects?done=saved#project-acme--api"


def test_settings_save_and_return_to_their_sections(web: Web) -> None:
    web.set_up()
    csrf = web.csrf()
    runs = web.client.post("/settings/runs", data={"csrf": csrf, "fanout": "1"}, headers=SITE)
    assert runs.headers["Location"] == "/settings?done=saved#runs"
    notify = web.client.post("/settings/notify", data={"csrf": csrf, "language": "en"}, headers=SITE)
    assert notify.headers["Location"] == "/settings?done=saved#notify"


def test_the_optional_sources_share_one_intro(web: Web) -> None:
    web.set_up()
    for lang, shared in (("en", "the tests cover them"), ("es", "los tests las cubren")):
        page = web.client.get("/settings", headers={"Accept-Language": lang}).get_data(as_text=True)
        assert page.count(shared) == 1  # the group says it once, not each of the three sources
        for key in ("signals", "logs", "loki"):
            assert f'id="{key}-title"' in page


def test_settings_say_what_the_events_wait_for_and_preselect_the_pages_language(web: Web) -> None:
    web.set_up()
    spanish = {"Accept-Language": "es"}
    page = web.client.get("/settings", headers=spanish).get_data(as_text=True)
    assert "Se aplican cuando añadas un destino arriba." in page
    assert '<option value="es" selected>' in page  # nothing saved yet: the page's language
    web.store.set_setting("notify_language", "en")
    page = web.client.get("/settings", headers=spanish).get_data(as_text=True)
    assert '<option value="en" selected>' in page and '<option value="es" selected>' not in page  # the saved choice
    web.client.post(
        "/settings/notify/add",
        data={"csrf": web.csrf(), "service": "ntfy", "topic": "lastcall-abc", "server": "https://ntfy.sh"},
        headers=SITE,
    )
    assert "They apply once you add a destination above." not in web.client.get("/settings").get_data(as_text=True)


def test_a_connected_integration_drops_its_intro(tmp_path: Path) -> None:
    from lastcall import signals

    web = Web(tmp_path, github=FakeGitHub())
    web.set_up()
    page = web.client.get("/settings").get_data(as_text=True)
    assert "lastcall works through your own GitHub App" not in page  # the App exists
    assert "lastcall can read the errors your apps send" in page  # not connected yet: the intro is there
    signals.Tracker("https://glitchtip.example.com", "acme", "good-token").save(web.secrets)
    page = web.client.get("/settings").get_data(as_text=True)
    assert 'class="status-line">Connected to https://glitchtip.example.com' in page
    assert "lastcall can read the errors your apps send" not in page


def github_users(kinds: Mapping[str, str]) -> Callable[[Request], Response]:
    """GitHub's public API for /users/<name>: the type of each name in `kinds`, 404 for any other, and no network
    at all for the name `offline` and a connection that drops mid-answer for `dropped`."""

    def fetch(req: Request) -> Response:
        prefix = "https://api.github.com/users/"
        assert req.full_url.startswith(prefix) and not req.has_header("Authorization")
        name = req.full_url.removeprefix(prefix)
        if name == "offline":
            raise OSError("no route to host")
        if name == "dropped":
            raise http.client.IncompleteRead(b'{"ty')
        if name == "limited":
            return 403, {}, b'{"message": "API rate limit exceeded"}'
        if name not in kinds:
            return 404, {}, b'{"message": "Not Found"}'
        return 200, {}, json.dumps({"login": name, "type": kinds[name]}).encode()

    return fetch


ACME = {"acme": "Organization", "octocat": "User"}
CREATE_FORM = r'<form method="post" action="(https://github\.com[^"]+)"'


def test_the_github_page_asks_one_question_and_has_no_checkbox(web: Web) -> None:
    web.set_up()
    page = web.client.get("/github").get_data(as_text=True)
    assert 'for="owner">Who owns the repositories that lastcall will work on?</label>' in page
    assert 'id="owner-hint"' in page and 'aria-describedby="owner-hint"' in page
    assert "Your GitHub username, or the name of the organization they belong to." in page
    assert 'placeholder="octocat"' in page and 'autocomplete="off"' in page
    assert 'autocapitalize="none"' in page and 'spellcheck="false"' in page
    assert 'type="checkbox"' not in page and "It is an organization" not in page and "help-github-org" not in page
    spanish = web.client.get("/github", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "¿De quién son los repositorios con los que trabajará lastcall?" in spanish
    assert "lastcall pregunta a GitHub cuál de las dos es." in spanish


def test_an_organization_found_by_github_leads_to_the_organizations_settings(tmp_path: Path) -> None:
    web = Web(tmp_path, fetch=github_users(ACME))
    web.set_up()
    response = web.client.get("/github/new?owner=acme")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "acme is an organization." in page
    assert "You need to be an owner of acme on GitHub" in page and "Sign in to github.com as" not in page
    action = re.search(CREATE_FORM, page)
    assert action and action.group(1).startswith("https://github.com/organizations/acme/settings/apps/new?state=")
    assert "You said" not in page
    spanish = web.client.get("/github/new?owner=acme", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "acme es una organización." in spanish and "Tienes que ser propietario de acme en GitHub" in spanish
    personal = web.client.get("/github/new?owner=octocat", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Entra en github.com como octocat antes de continuar" in personal


def test_a_user_found_by_github_leads_to_the_personal_settings(tmp_path: Path) -> None:
    web = Web(tmp_path, fetch=github_users(ACME))
    web.set_up()
    page = web.client.get("/github/new?owner=octocat").get_data(as_text=True)
    assert "octocat is a personal account." in page
    # GitHub creates a personal App under whoever is signed in there: the page says so, and only for a person
    assert "Sign in to github.com as octocat before you continue" in page
    assert "You need to be an owner of" not in page
    action = re.search(CREATE_FORM, page)
    assert action and action.group(1).startswith("https://github.com/settings/apps/new?state=")
    assert "GitHub will create a private App named lastcall-octocat for octocat" in page


def test_a_name_that_github_does_not_know_is_an_error_that_keeps_the_text(tmp_path: Path) -> None:
    web = Web(tmp_path, fetch=github_users(ACME))
    web.set_up()
    response = web.client.get("/github/new?owner=acmee")
    page = response.get_data(as_text=True)
    assert response.status_code == 400
    assert re.search(r'role="alert">GitHub has no account or organization called acmee\. Check the spelling', page)
    assert 'name="owner"' in page and 'value="acmee"' in page and 'name="manifest"' not in page
    spanish = web.client.get("/github/new?owner=acmee", headers={"Accept-Language": "es"})
    assert spanish.status_code == 400
    assert "GitHub no tiene ninguna cuenta ni organización llamada acmee" in spanish.get_data(as_text=True)


@pytest.mark.parametrize("name", ["offline", "limited", "dropped"])
def test_when_github_cannot_say_the_owner_chooses_with_no_create_button(tmp_path: Path, name: str) -> None:
    web = Web(tmp_path, fetch=github_users(ACME))
    web.set_up()
    response = web.client.get(f"/github/new?owner={name}")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f"GitHub did not say whether {name} is a person or an organization." in page
    assert f'<a class="button" href="/github/new?owner={name}&amp;org=0">It is a personal account</a>' in page
    assert f'<a class="button" href="/github/new?owner={name}&amp;org=1">It is an organization</a>' in page
    assert "Pick this if" in page and "Create the App on GitHub" not in page and 'name="manifest"' not in page
    assert 'role="alert"' not in page and "lastcall works through your own GitHub App" not in page
    chosen = web.client.get(f"/github/new?owner={name}&org=1").get_data(as_text=True)
    assert f"You said {name} is an organization." in chosen
    assert f"https://github.com/organizations/{name}/settings/apps/new?state=" in chosen
    personal = web.client.get(f"/github/new?owner={name}&org=0").get_data(as_text=True)
    assert (
        "https://github.com/settings/apps/new?state=" in personal
        and f"You said {name} is a personal account." in personal
    )
    in_spanish = {"Accept-Language": "es"}
    assert f"Has indicado que {name} es una organización." in web.client.get(
        f"/github/new?owner={name}&org=1", headers=in_spanish
    ).get_data(as_text=True)
    assert f"Has indicado que {name} es una cuenta personal." in web.client.get(
        f"/github/new?owner={name}&org=0", headers=in_spanish
    ).get_data(as_text=True)
    spanish = web.client.get(f"/github/new?owner={name}", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "Es una cuenta personal</a>" in spanish and "Es una organización</a>" in spanish


def test_an_explicit_org_never_asks_github(tmp_path: Path) -> None:
    def never(req: Request) -> Response:
        raise AssertionError("GitHub was asked")

    web = Web(tmp_path, fetch=never)
    web.set_up()
    for org in ("0", "1"):
        assert web.client.get(f"/github/new?owner=acme&org={org}").status_code == 200


@pytest.mark.parametrize(
    "typed", ["acme", " acme ", "@acme", "https://github.com/acme/", "github.com/acme", "https://github.com/@acme/"]
)
def test_the_owner_is_cleaned_before_it_is_checked(tmp_path: Path, typed: str) -> None:
    web = Web(tmp_path, fetch=github_users(ACME))
    web.set_up()
    page = web.client.get("/github/new", query_string={"owner": typed}).get_data(as_text=True)
    assert "acme is an organization." in page and "lastcall-acme" in page


def test_the_log_says_where_the_web_ui_answers(monkeypatch: pytest.MonkeyPatch, web: Web) -> None:
    monkeypatch.setattr(waitress, "serve", lambda *args, **options: None)
    for shown, expected in (
        (None, "Web UI on http://127.0.0.1:8080/ (in English and Spanish)"),
        ("http://192.168.1.20:9000/", "Web UI on http://192.168.1.20:9000/ (in English and Spanish)"),
    ):
        web.logs.clear()
        serve(web.app, "0.0.0.0", 8080, log=web.logs.append, shown=shown)
        assert web.logs == [expected]
    web.logs.clear()
    serve(web.app, "10.0.0.5", 8081, log=web.logs.append)
    assert web.logs == ["Web UI on http://10.0.0.5:8081/ (in English and Spanish)"]


def test_the_github_page_has_steps_a_way_back_and_its_help(tmp_path: Path) -> None:
    web = Web(tmp_path)
    web.set_up()
    page = web.client.get("/github").get_data(as_text=True)
    assert 'class="crumbs"' in page and 'href="/settings#github">Settings</a>' in page
    assert '<nav class="crumbs" aria-label="Breadcrumb">' in page and "You are here" not in page
    assert 'aria-label="Ruta de navegación"' in web.client.get("/github", headers={"Accept-Language": "es"}).get_data(
        as_text=True
    )
    assert page.count('<li aria-current="step">Create</li>') == 1
    assert "lastcall works through your own GitHub App" in page  # the intro, in the default branch
    for topic in ("app", "owner"):
        assert (
            f'class="help" popovertarget="help-github-{topic}"' in page
            and f'id="help-github-{topic}" popover' in page
        )
    confirm = web.client.get("/github/new?owner=acme&org=1").get_data(as_text=True)
    assert "lastcall works through your own GitHub App" not in confirm
    assert "help-github-permissions" in confirm and "help-github-create" in confirm
    ready = Web(tmp_path / "ready", github=FakeGitHub())
    ready.set_up()
    page = ready.client.get("/github").get_data(as_text=True)
    assert 'class="status-line"' in page and 'href="/github/installed">Next: choose your projects</a>' in page
    assert '<li class="is-done">Create' in page and "help-github-install" in page
    assert "lastcall works through your own GitHub App" not in page
    done = ready.client.get("/github?done=app_created").get_data(as_text=True)
    assert 'class="notice done readable" role="status"' in done  # the notices keep the card's 44rem
    bad = ready.client.get("/github/new?owner=not%20a%20login").get_data(as_text=True)
    assert 'class="notice error readable" role="alert"' in bad
    settings = ready.client.get("/settings").get_data(as_text=True)  # the ready sentence, like the other integrations
    assert re.search(
        r'<p class="status-line">lastcall-test belongs to acme\.</p>\s*<div class="line"><a href="https://', settings
    )


def test_setup_and_login_explain_themselves(web: Web) -> None:
    page = web.client.get("/setup").get_data(as_text=True)
    assert page.count('<ol class="steps sign-steps">') == 1 and 'placeholder="XXXX-XXXX"' in page
    assert 'minlength="12"' in page and 'aria-describedby="password-hint"' in page
    for topic in ("setup-why", "setup-code", "setup-password"):
        assert f'popovertarget="help-{topic}"' in page and f'id="help-{topic}" popover' in page
    assert "help-login-locked" not in page  # no error, no bubble
    assert 'hreflang="es">Español</a>' in page  # the signed-out header has the language switch
    web.set_up()
    other = web.app.test_client()
    page = other.get("/login").get_data(as_text=True)
    assert "<details" in page and 'autocomplete="current-password"' in page and "autofocus" in page
    for topic in ("login-session", "login-reset"):
        assert f'popovertarget="help-{topic}"' in page
    assert "help-login-locked" not in page
    assert 'hreflang="es">Español</a>' in page  # and so does the signed-out header of /login
    wrong = other.post("/login", data={"password": "wrong"}, headers=SITE).get_data(as_text=True)
    assert 'popovertarget="help-login-locked"' in wrong


def test_setup_explains_locking_only_when_the_attempt_counted(tmp_path: Path) -> None:
    web = Web(tmp_path)
    page = web.client.get("/setup").get_data(as_text=True)
    form = {"code": web.code(), "password": PASSWORD, "repeat": PASSWORD + "x"}
    mismatch = web.client.post("/setup", data=form, headers=SITE)
    text = mismatch.get_data(as_text=True)
    assert mismatch.status_code == 400 and "not the same" in text and "help-login-locked" not in text
    short = web.client.post("/setup", data={**form, "password": "short", "repeat": "short"}, headers=SITE)
    assert short.status_code == 400 and "help-login-locked" not in short.get_data(as_text=True)
    assert "help-login-locked" not in page
    wrong = web.client.post("/setup", data={**form, "code": "WRNG-CODE"}, headers=SITE)
    assert wrong.status_code == 400 and 'popovertarget="help-login-locked"' in wrong.get_data(as_text=True)


def test_the_error_page_leads_back_and_explains_its_cause(tmp_path: Path) -> None:
    web = Web(tmp_path, origins=("https://lastcall.example.com",))
    web.set_up()
    other = web.app.test_client()
    page = other.post("/login", data={"password": PASSWORD}).get_data(as_text=True)  # no Origin: refused
    assert "Form not accepted" in page and 'popovertarget="help-error-origin"' in page
    assert 'href="/login">Back to sign in</a>' in page
    assert 'class="card narrow sign-card edge-bad"' in page  # the edge says that something went wrong
    gone = other.get("/nowhere").get_data(as_text=True)
    assert 'href="/login">Back to sign in</a>' in gone and "help-error" not in gone
    site = {"Origin": "https://lastcall.example.com"}
    forged = web.client.post("/service/pause", data={"csrf": "forged"}, headers=site)
    page = forged.get_data(as_text=True)
    assert forged.status_code == 403 and "Form expired" in page and 'popovertarget="help-error-csrf"' in page
    assert 'href="/">Back to the dashboard</a>' in page


def test_the_sign_cards_keep_their_commands_small_and_the_error_edge_red() -> None:
    css = (Path(__file__).parent.parent / "src/lastcall/web/static/style.css").read_text(encoding="utf-8")
    assert ".sign-card pre, .sign-card pre code { font-size: .84rem; }" in css  # the code inside the pre too
    assert ".card.edge-bad { --edge: var(--bad); }" in css


def test_the_layout_never_makes_the_page_wider_than_the_screen() -> None:
    css = (Path(__file__).parent.parent / "src/lastcall/web/static/style.css").read_text(encoding="utf-8")
    body = re.search(r"\nbody \{(.*?)\n\}", css, re.S)
    assert body is not None and "grid-template-columns: minmax(0, 1fr);" in body.group(1)  # no line widens the page
    # the notice sticks only where it stays one line, and the page then keeps its room clear
    sticky = re.search(r"@media \(min-width: 48rem\) and \(min-height: 36rem\) \{(.*?)\n\}", css, re.S)
    assert sticky is not None and "position: sticky" in sticky.group(1)
    assert "scroll-padding-top: 6rem" in sticky.group(1)
    assert css.count("position: sticky; top: var(--s2)") == 1
    # a long repository wraps in its column; only the short dates of the runs stay on one line
    assert ".runs-table tbody th { white-space: nowrap; }" in css
    assert not re.search(r"\n  tbody th \{ white-space: nowrap", css)
    assert "position-try-order: most-height" in css and "max-height: calc(100% - 1rem)" in css  # it scrolls inside
    assert ".sign-card pre { white-space: pre-wrap; overflow-wrap: anywhere; }" in css  # a command is read whole


def test_a_projects_findings_keep_their_help_beside_them(github_web: Web) -> None:
    github_web.store.add_project("acme/api")
    page = github_web.client.get("/projects").get_data(as_text=True)
    # the text wraps in its own box, and its "?" is never left alone on a line
    assert '<dd><span class="with-help"><span>none yet</span><button type="button" class="help"' in page
    assert 'aria-label="Help: Findings">?</button></span></dd>' in page


def test_a_reading_from_before_its_reset_never_says_a_burst_is_on(web: Web) -> None:
    web.store.add_account("claude", "claude-code", "English", 10)
    week = Window("week", 68, NOW - 25 * HOUR, "expiring", 168.0)  # read 2 days ago, before the weekly reset
    web.store.record_reading(Reading("claude", (week,), False, "exact", NOW - 2 * DAY))
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert "reset passed" in page and "reset passed, waiting" not in page
        assert 'class="plan is-held"' in page  # a plan that says nothing bursts is muted, like a held one
        assert "Burst in progress" not in page and ">Bursting</span>" not in page and "is-live" not in page
        assert "No burst: the window has reset since this reading, and what it had left is gone." in page
    web.client.get("/language/es")
    page = web.client.get("/").get_data(as_text=True)
    assert "Sin ráfaga: la ventana se ha reiniciado desde esta lectura y lo que le quedaba se ha perdido." in page


def test_a_key_account_without_its_key_cannot_spend_on_either_page(web: Web) -> None:
    from lastcall.accounts import tier_models

    web.store.add_account(
        "zen", "opencode", provider="openrouter", models=tier_models("qwen3-coder"), policy="budget", budget_usd=20
    )
    web.store.record_spend("zen", 6.4, "deep", "r0", NOW - 60)  # behind the pace of the month: its plan would burst
    web.store.set_setting("heartbeat", str(NOW))
    web.set_up()
    missing = "Its API key is missing: this account spends nothing, not even on runs you start, until you give"
    once = "This account has no API key, so it spends nothing, not even on runs you start: give it one below."
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert ">Needs a key</span>" in page and ">Bursting</span>" not in page, path
        assert "Burst in progress" not in page and "is-live" not in page, path
    page = web.client.get("/").get_data(as_text=True)
    assert missing in page and once not in page
    page = web.client.get("/accounts").get_data(as_text=True)  # one message, with its pointer to the field below
    assert once in page and missing not in page and 'class="plan' not in page
    web.secrets.set("key-zen", "sk-or-test")
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert ">Needs a key</span>" not in page and ">Bursting</span>" in page and "Burst in progress" in page
    web.secrets.delete("key-zen")
    web.client.get("/language/es")
    page = web.client.get("/").get_data(as_text=True)
    assert ">Falta la clave</span>" in page and "Le falta su clave de API: esta cuenta no gasta nada" in page


def test_a_burst_due_waits_for_the_run_the_owner_started(web: Web) -> None:
    web.store.add_account("claude", "claude-code", "English", 10)
    week = Window("week", 62, NOW + 10 * HOUR, "expiring", 168.0)  # 28% left, 10 h to the reset: a burst is due
    web.store.record_reading(Reading("claude", (week,), False, "exact", NOW - 600))
    web.set_up()
    web.store.set_setting("heartbeat", str(NOW))
    web.store.start_run("r1", "acme/api", "claude", "manual")
    due = "Burst due: 28% left and 10\u00a0h to the reset. It starts when the run under way finishes."
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert ">Working</span>" in page and due in page and "Burst in progress" not in page, path
        assert "is-live" not in page, path
    web.client.get("/language/es")
    page = web.client.get("/").get_data(as_text=True)
    assert "Ráfaga pendiente: queda 28\u00a0% y faltan 10\u00a0h para el reinicio. Empieza cuando termine" in page
    web.store.finish_run("r1", "ok", None)
    web.store.start_run("r2", "acme/api", "claude", "burst")  # the burst itself
    web.client.get("/language/en")
    page = web.client.get("/").get_data(as_text=True)
    assert "Burst in progress: 28% left and 10\u00a0h to the reset." in page and "Burst due" not in page


def test_an_account_whose_quota_cannot_be_read_says_so_on_both_pages(tmp_path: Path) -> None:
    from lastcall.sandbox import SandboxError

    def read(account: Account) -> Reading:
        raise SandboxError("docker is not running")

    web = Web(tmp_path, read_quota=read)
    web.store.add_account("claude", "claude-code", "English", 10)
    web.store.set_setting("heartbeat", str(NOW))
    web.set_up()
    response = web.client.post("/accounts/claude/quota", data={"csrf": web.csrf()}, headers=SITE)
    assert response.status_code == 302
    for path in ("/", "/accounts"):
        assert ">Cannot read</span>" in web.client.get(path).get_data(as_text=True), path
    assert "docker is not running" in web.client.get("/accounts").get_data(as_text=True)
    web.client.get("/language/es")
    for path in ("/", "/accounts"):
        assert ">Sin lectura</span>" in web.client.get(path).get_data(as_text=True), path


def test_the_card_stops_saying_cannot_read_once_a_newer_reading_is_stored(tmp_path: Path) -> None:
    from lastcall.sandbox import SandboxError

    def read(account: Account) -> Reading:
        raise SandboxError("docker is not running")

    web = Web(tmp_path, read_quota=read)
    web.store.add_account("claude", "claude-code", "English", 10)
    web.store.set_setting("heartbeat", str(NOW))
    web.set_up()
    web.client.post("/accounts/claude/quota", data={"csrf": web.csrf()}, headers=SITE)

    def says_cannot_read() -> list[bool]:
        return [">Cannot read</span>" in web.client.get(path).get_data(as_text=True) for path in ("/", "/accounts")]

    def stored(taken_at: float) -> None:
        window = Window("week", 12, NOW + 30 * HOUR, "expiring", 168.0)
        web.store.record_reading(Reading("claude", (window,), False, "exact", taken_at))

    assert says_cannot_read() == [True, True]
    stored(NOW - 60)  # a reading from before the failure does not undo it
    assert says_cannot_read() == [True, True]
    stored(NOW + 900)  # the service has read it fine since
    assert says_cannot_read() == [False, False]
    assert "docker is not running" not in web.client.get("/accounts").get_data(as_text=True)


def test_an_unexpected_error_is_logged_by_its_type_and_never_by_its_repr(tmp_path: Path) -> None:
    from lastcall import loki

    secret = "Authorization: Bearer glc_s3cret"

    def unexpected(*args: Any) -> Any:
        raise RuntimeError("request failed" + chr(10) + f"headers={{{secret!r}}}")

    web = Web(tmp_path, containers=unexpected, loki_fetch=unexpected)
    web.set_up()
    loki.Loki("http://loki.example.com:3100", token="glc_s3cret").save(web.secrets)
    page = web.client.get("/settings")
    assert page.status_code == 200 and "glc_s3cret" not in page.get_data(as_text=True)
    said = [line for line in web.logs if "could not be listed" in line]
    assert len(said) == 2 and all(chr(10) not in line and "RuntimeError" in line for line in said)
    assert not any("glc_s3cret" in line for line in web.logs)


def test_brief_keeps_the_type_and_the_start_of_the_message() -> None:
    from lastcall.web import brief

    assert brief(KeyError("id")) == "KeyError: 'id'"
    assert brief(ValueError()) == "ValueError"
    assert brief(ValueError("a" + chr(10) + "  b")) == "ValueError: a b"
    assert len(brief(ValueError("x" * 1000))) < 150
    assert brief(ValueError('Invalid header value b"Bearer abc def"')) == "ValueError: Invalid header value ***"
    assert brief(OSError("401 for Bearer abc def")) == "OSError: 401 for bearer ***"
    assert "abc" not in brief(OSError("sent Authorization: Basic abc"))


def test_loki_refuses_a_login_with_a_line_break_inside(tmp_path: Path) -> None:
    from lastcall import loki

    asked: list[str] = []

    def fetch(url: str, headers: Mapping[str, str]) -> Any:
        asked.append(url)
        return {"status": "success", "data": []}

    web = Web(tmp_path, loki_fetch=fetch)
    web.set_up()
    form = {"csrf": web.csrf(), "url": "http://loki.example.com:3100", "user": "", "tenant": ""}
    for field, value in (("token", "glc_a" + chr(10) + "glc_b"), ("user", "1234" + chr(13) + "56")):
        response = web.client.post("/settings/loki", data={**form, "token": "t", field: value}, headers=SITE)
        page = response.get_data(as_text=True)
        assert response.status_code == 400 and "cannot hold line breaks" in page and "glc_b" not in page
    web.client.get("/language/es")
    spanish = web.client.post("/settings/loki", data={**form, "token": "a" + chr(10) + "b"}, headers=SITE)
    assert "no pueden llevar saltos de línea" in spanish.get_data(as_text=True)
    assert asked == [] and loki.Loki.load(web.secrets) is None


def test_loki_turns_a_header_that_cannot_be_sent_into_a_form_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(request: Any, timeout: float) -> None:
        raise ValueError('Invalid header value b"Bearer glc_s3cret"')

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    web = Web(tmp_path)  # the real fetch, with a token that only the encoding of the header refuses
    web.set_up()
    form = {
        "csrf": web.csrf(),
        "url": "http://loki.example.com:3100",
        "user": "",
        "token": "glc_s3cret",
        "tenant": "",
    }
    response = web.client.post("/settings/loki", data=form, headers=SITE)
    page = response.get_data(as_text=True)
    assert response.status_code == 400 and "could not be sent" in page and "glc_s3cret" not in page
    assert not any("glc_s3cret" in line for line in web.logs)


def test_github_asks_once_for_an_owner_every_10_minutes(tmp_path: Path) -> None:
    asked: list[str] = []
    answers = github_users(ACME)

    def fetch(req: Request) -> Response:
        asked.append(req.full_url.rsplit("/", 1)[1])
        return answers(req)

    web = Web(tmp_path, fetch=fetch)
    web.set_up()
    for owner in ("acme", "acme", "ACME", "octocat", "nobody", "nobody", "offline", "offline", "limited"):
        web.client.get(f"/github/new?owner={owner}")
    # an answer is kept: user, org and missing; "GitHub did not say" never
    assert asked == ["acme", "octocat", "nobody", "offline", "offline", "limited"]
    web.clock.now += 599
    assert "acme is an organization." in web.client.get("/github/new?owner=acme").get_data(as_text=True)
    assert asked.count("acme") == 1
    web.clock.now += 2
    web.client.get("/github/new?owner=acme")
    assert asked.count("acme") == 2


def test_github_forms_waiting_for_an_answer_are_limited_to_the_newest_50(tmp_path: Path) -> None:
    def exchange(code: str) -> dict[str, Any]:
        return {
            "id": 42,
            "slug": "lastcall-acme",
            "name": "lastcall-acme",
            "owner": {"login": "acme"},
            "html_url": "https://github.com/apps/lastcall-acme",
            "pem": "-----BEGIN RSA PRIVATE KEY-----",
        }

    web = Web(tmp_path, exchange=exchange)
    web.set_up()
    states = []
    for _ in range(60):  # every visit to the page makes one, and an hour has to pass before one is forgotten
        page = web.client.get("/github/new?owner=acme&org=1").get_data(as_text=True)
        action = re.search(CREATE_FORM, page)
        assert action
        states.append(parse_qs(urlsplit(action.group(1).replace("&amp;", "&")).query)["state"][0])
    callback = "/github/callback?code=abc&state="
    assert [web.client.get(callback + states[n]).status_code for n in (0, 9)] == [400, 400]
    assert [web.client.get(callback + states[n]).status_code for n in (10, 59)] == [302, 302]


def test_the_log_brackets_an_ipv6_address(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(waitress, "serve", lambda *args, **options: None)
    web = Web(tmp_path)
    for host, said in (
        ("127.0.0.1", "http://127.0.0.1:8080/"),
        ("lastcall.lan", "http://lastcall.lan:8080/"),
        ("::1", "http://[::1]:8080/"),
        ("0.0.0.0", "http://127.0.0.1:8080/"),
        ("::", "http://127.0.0.1:8080/"),
    ):
        web.logs.clear()
        serve(web.app, host, 8080, log=web.logs.append)
        assert web.logs == [f"Web UI on {said} (in English and Spanish)"], host


# an App made before v0.7.2 was not granted Checks and Commit statuses: the card of a project that needs them says so

LACKING = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}
GRANTED = {**LACKING, "checks": "read", "statuses": "read"}
ARROW = chr(0x203A)  # the single right-pointing angle quotation mark of "Settings > Applications"
INSTALLED = f"Settings {ARROW} Applications {ARROW} Installed GitHub Apps {ARROW} Configure"  # where GitHub says so


@pytest.fixture(scope="module")
def app_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def installation(login: str, kind: str, permissions: Mapping[str, str]) -> tuple[int, object]:
    """GitHub's answer for the App's installation on a repository's owner."""
    return 200, {"id": 7, "account": {"login": login, "type": kind}, "target_type": kind, "permissions": permissions}


class Installations:
    """GitHub's /repos/<owner>/<name>/installation, asked with the App's own token: the answer of each owner that
    has one (an exception is raised, as a network that fails does), 404 for any other. It records what was asked."""

    PREFIX = "https://api.github.com/repos/"

    def __init__(self, answers: Mapping[str, tuple[int, object] | Exception]) -> None:
        self.answers = dict(answers)
        self.asked: list[Request] = []

    def __call__(self, req: Request) -> Response:
        self.asked.append(req)
        assert req.full_url.startswith(self.PREFIX) and req.full_url.endswith("/installation"), req.full_url
        assert (req.get_header("Authorization") or "").startswith("Bearer ")  # the App's token, not an installation's
        answer = self.answers.get(self.owner(req), (404, {"message": "Not Found"}))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return status, {}, body if isinstance(body, bytes) else json.dumps(body).encode()

    def owner(self, req: Request) -> str:
        return req.full_url.removeprefix(self.PREFIX).split("/")[0].lower()  # GitHub does not tell cases apart

    def owners(self) -> list[str]:
        return [self.owner(req) for req in self.asked]


def turn_on(web: Web, repo: str, merge_on_approve: bool = True, watch: bool = False) -> None:
    if web.store.project(repo) is None:
        web.store.add_project(repo)
    web.store.update_project(
        repo, active=True, weight=1, language=None, max_prs=3, merge_on_approve=merge_on_approve, watch=watch
    )


def checks_web(tmp_path: Path, pem: str, github: Installations, *repos: str, owner: str = "acme") -> Web:
    """A signed-in web UI, with the App of `owner`, and a project with merge on approve for each of `repos`."""
    web = Web(tmp_path, fetch=github)
    App(42, "lastcall-acme", "lastcall-acme", owner, "https://github.com/apps/lastcall-acme", pem).save(web.secrets)
    web.set_up()
    for repo in repos:
        turn_on(web, repo)
    return web


def card(page: str, repo: str) -> str:
    return page.split(f'id="project-{repo.replace("/", "--")}"')[1].split("</article>")[0]


def notices(page: str) -> list[str]:
    """The text of each notice about checks on the page."""
    found = re.findall(r'<div class="notice checks" role="note">(.*?)</div>', page, re.S)
    return [html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", n))).strip() for n in found]


@pytest.mark.parametrize(
    ("owner", "kind", "link"),
    [
        ("octocat", "User", "https://github.com/settings/apps/lastcall-acme/permissions"),
        ("acme", "Organization", "https://github.com/organizations/acme/settings/apps/lastcall-acme/permissions"),
    ],
)
def test_a_project_that_merges_or_watches_says_what_the_app_lacks_and_where_to_fix_it(
    tmp_path: Path, app_pem: str, owner: str, kind: str, link: str
) -> None:
    github = Installations({owner: installation(owner, kind, LACKING)})
    web = checks_web(tmp_path, app_pem, github, f"{owner}/api", owner=owner)
    turn_on(web, f"{owner}/lib", merge_on_approve=False, watch=True)  # the watch needs them too
    web.store.add_project(f"{owner}/web")  # neither: nothing to say
    response = web.client.get("/projects")
    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert len(notices(page)) == 2 and notices(card(page, f"{owner}/web")) == []
    for repo in (f"{owner}/api", f"{owner}/lib"):
        assert notices(card(page, repo)) == [
            "“Merge after my approval” and “Watch after the merge” need the GitHub App to read checks and commit "
            "statuses, and this App cannot yet. Open the App's permissions on GitHub. Set “Checks” and “Commit "
            "statuses” to “Read-only” and save. Accept the new permissions where GitHub asks for it, on the App's "
            f"installation: {INSTALLED}, or the organization's settings."
        ]
        block = re.findall(r'<div class="notice checks".*?</div>', card(page, repo), re.S)[0]
        assert [html.unescape(url) for url in re.findall(r'<a href="([^"]+)"', block)] == [link]  # the same tab
        assert not re.search(r"\s(target|style|on[a-z]+)=", block)  # what the content security policy would block
    assert github.owners() == [owner]  # one installation holds all of the owner's repositories


def test_the_notice_in_spanish_quotes_the_labels_of_the_two_settings(tmp_path: Path, app_pem: str) -> None:
    github = Installations({"acme": installation("acme", "Organization", LACKING)})
    web = checks_web(tmp_path, app_pem, github, "acme/api")
    page = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert notices(page) == [
        "«Fusionar tras mi aprobación» y «Vigilar tras la fusión» necesitan que la GitHub App lea los checks y los "
        "estados de commit, y esta App todavía no puede. Abre los permisos de la App en GitHub. Pon «Checks» y "
        "«Commit statuses» en «Read-only» y guarda. Acepta los nuevos permisos donde GitHub te lo pida, en la "
        f"instalación de la App: {INSTALLED}, o en los ajustes de la organización."
    ]
    assert 'name="merge_on_approve" value="1" checked> Fusionar tras mi aprobación' in page
    assert 'name="watch" value="1"> Vigilar tras la fusión' in page


@pytest.mark.parametrize("granted", [GRANTED, {**LACKING, "checks": "write", "statuses": "write"}])
def test_no_notice_when_the_installation_can_read_the_checks(
    tmp_path: Path, app_pem: str, granted: dict[str, str]
) -> None:
    github = Installations({"acme": installation("acme", "Organization", granted)})
    web = checks_web(tmp_path, app_pem, github, "acme/api")
    response = web.client.get("/projects")
    assert response.status_code == 200 and notices(response.get_data(as_text=True)) == []
    assert github.owners() == ["acme"]


def test_a_project_that_reads_no_checks_never_asks_github(tmp_path: Path, app_pem: str) -> None:
    github = Installations({})
    web = checks_web(tmp_path, app_pem, github)
    web.store.add_project("acme/api")
    response = web.client.get("/projects")
    assert response.status_code == 200 and notices(response.get_data(as_text=True)) == []
    assert github.asked == []


@pytest.mark.parametrize(
    ("answer", "logged"),
    [
        (OSError("no route to host"), True),
        (RuntimeError("boom"), True),
        ((403, {"message": "API rate limit exceeded"}), True),
        ((500, {}), True),
        ((404, {"message": "Not Found"}), True),  # the App is not installed there
        ((200, b"<html>not json</html>"), True),
        ((200, {"id": 7, "account": {"login": "acme", "type": "Organization"}}), False),  # it does not say
        (installation("other", "Organization", LACKING), False),  # not the App owner's account: no address
        (installation("acme", "Bot", LACKING), False),
    ],
)
def test_when_github_cannot_say_the_page_shows_nothing_and_does_not_fail(
    tmp_path: Path, app_pem: str, answer: tuple[int, object] | Exception, logged: bool
) -> None:
    github = Installations({"acme": answer})
    web = checks_web(tmp_path, app_pem, github, "acme/api")
    response = web.client.get("/projects")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and notices(page) == []
    assert 'name="merge_on_approve" value="1" checked>' in page  # the rest of the card is there
    assert any("could not be read" in line for line in web.logs) == logged
    assert not any("BEGIN" in line for line in web.logs)  # never the key


def test_a_key_that_cannot_sign_never_breaks_the_page(tmp_path: Path) -> None:
    web = Web(tmp_path, github=FakeGitHub())  # its App has "not a key" for a key
    web.set_up()
    turn_on(web, "acme/api")
    response = web.client.get("/projects")
    assert response.status_code == 200 and notices(response.get_data(as_text=True)) == []
    assert any("could not be read" in line for line in web.logs)


def test_the_page_waits_5_seconds_at_most_for_github(tmp_path: Path, app_pem: str) -> None:
    github = Installations({"acme": installation("acme", "Organization", LACKING)})
    checks_web(tmp_path, app_pem, github, "acme/api").client.get("/projects")
    assert GRANT_TIMEOUT == 5 and [getattr(req, "timeout", None) for req in github.asked] == [5]


def test_a_notice_is_asked_again_after_a_minute_and_what_is_granted_after_ten(tmp_path: Path, app_pem: str) -> None:
    github = Installations({"acme": installation("acme", "Organization", LACKING)})
    web = checks_web(tmp_path, app_pem, github, "acme/api")
    assert len(notices(web.client.get("/projects").get_data(as_text=True))) == 1
    github.answers["acme"] = installation("acme", "Organization", GRANTED)  # the owner accepted the new permissions
    web.clock.now += 59
    assert len(notices(web.client.get("/projects").get_data(as_text=True))) == 1 and len(github.asked) == 1
    web.clock.now += 2
    assert notices(web.client.get("/projects").get_data(as_text=True)) == [] and len(github.asked) == 2
    web.clock.now += 598  # what it has been granted is kept 10 minutes
    assert notices(web.client.get("/projects").get_data(as_text=True)) == [] and len(github.asked) == 2
    web.clock.now += 2
    web.client.get("/projects")
    assert len(github.asked) == 3


def test_a_github_that_does_not_answer_is_asked_again_after_a_minute_not_at_every_load(
    tmp_path: Path, app_pem: str
) -> None:
    github = Installations({"acme": OSError("no route to host")})
    web = checks_web(tmp_path, app_pem, github, "acme/api")
    for _ in range(3):
        assert web.client.get("/projects").status_code == 200
    assert len(github.asked) == 1  # three loads, one wait
    web.clock.now += 61
    github.answers["acme"] = installation("acme", "Organization", LACKING)
    assert len(notices(web.client.get("/projects").get_data(as_text=True))) == 1 and len(github.asked) == 2


def test_what_github_says_about_installations_is_kept_for_a_bounded_number_of_owners(
    tmp_path: Path, app_pem: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(site, "MAX_GRANTS", 3)
    owners = [f"o{n}" for n in range(1, 6)]
    github = Installations({o: installation(o, "Organization", GRANTED) for o in owners})
    web = checks_web(tmp_path, app_pem, github, *(f"{o}/api" for o in owners), owner="o1")
    web.client.get("/projects")
    assert github.owners() == owners  # a project of each: five installations asked, the newest three kept
    for only in ("o5", "o3"):  # still known: no new request
        for o in owners:
            turn_on(web, f"{o}/api", merge_on_approve=o == only)
        web.client.get("/projects")
        assert github.owners() == owners, only
    for o in owners:
        turn_on(web, f"{o}/api", merge_on_approve=o == "o2")
    web.client.get("/projects")
    assert github.owners() == [*owners, "o2"]  # the oldest was dropped: asked again


def test_the_owner_of_a_repository_is_asked_without_regard_to_case(tmp_path: Path, app_pem: str) -> None:
    github = Installations({"acme": installation("Acme", "Organization", LACKING)})
    web = checks_web(tmp_path, app_pem, github, "Acme/api", "acme/web", owner="acme")
    assert len(notices(web.client.get("/projects").get_data(as_text=True))) == 2 and len(github.asked) == 1


# easier first steps: GitHub brings the owner back after the installation, and many projects are added at once


class CountingGitHub(FakeGitHub):
    """The two repositories of FakeGitHub, and how many times they were read."""

    def __init__(self) -> None:
        super().__init__()
        self.readings = 0

    def installations(self) -> list[dict[str, Any]]:
        self.readings += 1
        return super().installations()


class NoGitHub(FakeGitHub):
    """A GitHub that fails the test as soon as anything asks it for anything."""

    def __getattribute__(self, name: str) -> Any:
        if name in ("installations", "installation_token", "repositories", "files", "repository_token", "repository"):
            raise AssertionError(f"GitHub was asked: {name}")
        return super().__getattribute__(name)


def never_fetch(req: Request) -> Response:
    raise AssertionError(f"GitHub was asked: {req.full_url}")


SUGGESTED = ("acme/api", "acme/web", "acme/Worker", "acme/api-client")
OTHERS = ("acme/docs", "acme/old-site", "acme/legacy-API")


def suggest_many(store: Store, suggested: Collection[str] = SUGGESTED, others: Collection[str] = OTHERS) -> None:
    """What a reading of GitHub left in the store: the repositories, the first ones recommended."""
    why: tuple[tuple[str, dict[str, Any]], ...] = (("push", {"count": 3}), ("tests", {}))
    found = [Suggestion(name, 60 - n, True, "Python", 8_000, why) for n, name in enumerate(suggested)]
    found += [Suggestion(name, 20 - n, False, "Go", 3_000, why) for n, name in enumerate(others)]
    store.save_suggestions(found, NOW)


def many_web(tmp_path: Path, **suggestions: Collection[str]) -> Web:
    """A signed-in web UI whose GitHub fails the test when asked: Add many reads the store only."""
    web = Web(tmp_path, github=NoGitHub(), fetch=never_fetch)
    web.set_up()
    suggest_many(web.store, **suggestions)
    return web


def add_many(web: Web, pick: str, *repos: str, q: str | None = None) -> TestResponse:
    form: dict[str, Any] = {"csrf": web.csrf(), "pick": pick, "repo": list(repos)}
    if q is not None:
        form["q"] = q
    return web.client.post("/projects/add-many", data=form, headers=SITE)


def added(web: Web) -> set[str]:
    return {p.repo for p in web.store.projects()}


def pick_form(page: str) -> str:
    """The form that adds many projects, the only one of the Projects page that posts to /projects/add-many."""
    found = re.search(r'<form method="post" action="/projects/add-many" class="pick">(.*?)</form>', page, re.S)
    assert found, "the Projects page has no Add many form"
    return found.group(1)


def test_the_manifest_that_the_web_ui_sends_asks_github_to_come_back_and_the_app_remembers_it(tmp_path: Path) -> None:
    conversion = {
        "id": 42,
        "slug": "lastcall-acme",
        "name": "lastcall-acme",
        "owner": {"login": "acme"},
        "html_url": "https://github.com/apps/lastcall-acme",
        "pem": "-----BEGIN RSA PRIVATE KEY-----",
    }
    web = Web(tmp_path, exchange=lambda code: conversion)
    web.set_up()
    page = web.client.get("/github/new?owner=acme&org=1").get_data(as_text=True)
    value = re.search(r'name="manifest" value="([^"]+)"', page)
    action = re.search(CREATE_FORM, page)
    assert value and action
    sent = json.loads(html.unescape(value.group(1)))
    assert sent["redirect_url"] == "http://localhost/github/callback"  # built like the redirect url
    assert sent["setup_url"] == "http://localhost/github/installed" and sent["setup_on_update"] is True
    state = parse_qs(urlsplit(html.unescape(action.group(1))).query)["state"][0]
    assert web.client.get(f"/github/callback?code=abc&state={state}").status_code == 302
    saved = App.load(web.secrets)
    assert saved is not None and saved.returns is True  # GitHub will bring the owner back after installing it


def test_coming_back_from_github_reads_the_repositories_and_goes_to_the_projects(tmp_path: Path) -> None:
    github = CountingGitHub()
    web = Web(tmp_path, github=github)
    web.set_up()
    assert web.store.suggestions() == [] and github.readings == 0
    web.logs.clear()
    # GitHub adds an installation_id, and anyone can write one: no part of the address is used
    for query in ("?installation_id=4242&setup_action=install", "?installation_id=not-a-number", ""):
        github.readings = 0
        response = web.client.get("/github/installed" + query)
        assert response.status_code == 302 and response.headers["Location"] == "/projects?done=installed"
        assert github.readings == 1
    assert [s.repo for s in web.store.suggestions()] == ["acme/api", "acme/old-site"]
    lines = [line for line in web.logs if "after its installation page" in line]
    assert lines == ["Web UI: reading the repositories of the GitHub App, after its installation page"] * 3
    assert "4242" not in "".join(web.logs)  # and never the id that anyone can write
    english = web.client.get("/projects?done=installed").get_data(as_text=True)
    assert (
        '<p class="notice done" role="status">lastcall is reading the repositories where your GitHub App is '
        "installed. Reload this page in a moment and tick the ones it may work on.</p>" in english
    )
    spanish = web.client.get("/projects?done=installed", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert (
        '<p class="notice done" role="status">lastcall está leyendo los repositorios donde está instalada tu '
        "GitHub App. Recarga esta página en un momento y marca aquellos en los que puede trabajar.</p>" in spanish
    )


def test_coming_back_from_github_needs_a_signed_in_owner_and_an_app(tmp_path: Path) -> None:
    web = Web(tmp_path / "none")
    assert web.client.get("/github/installed").headers["Location"] == "/setup"
    web.set_up()
    without_app = web.client.get("/github/installed?installation_id=7")
    assert without_app.status_code == 302 and without_app.headers["Location"] == "/github"
    assert web.store.suggestions() == []
    github = CountingGitHub()
    ready = Web(tmp_path / "app", github=github)
    ready.set_up()
    stranger = ready.app.test_client()
    assert stranger.get("/github/installed?installation_id=7").headers["Location"] == "/login"
    assert github.readings == 0 and ready.store.suggestions() == []  # nobody who is not signed in starts a reading


def test_one_reading_of_the_repositories_at_a_time_whoever_asks_for_it(tmp_path: Path) -> None:
    github = CountingGitHub()
    waiting: list[Callable[[], None]] = []
    web = Web(tmp_path, github=github, background=waiting.append)
    web.set_up()
    assert web.client.post("/projects/refresh", data={"csrf": web.csrf()}, headers=SITE).status_code == 302
    for _ in range(2):
        assert web.client.get("/github/installed").headers["Location"] == "/projects?done=installed"
    assert len(waiting) == 1 and github.readings == 0  # one is waiting to run: the others did not start another
    page = web.client.get("/projects?done=installed").get_data(as_text=True)
    assert "Reading from GitHub now. Reload this page in a minute." in page  # the page's own line stays
    waiting.pop()()  # the reading goes through
    assert github.readings == 1 and len(web.store.suggestions()) == 2
    web.client.get("/github/installed")
    assert len(waiting) == 1  # and the next one may start


@pytest.mark.parametrize("returns", [True, False])
def test_the_github_page_at_step_two_leads_to_the_installation_and_then_to_the_projects(
    tmp_path: Path, returns: bool
) -> None:
    web = Web(tmp_path, github=FakeGitHub())
    replace(APP, returns=returns).save(web.secrets)  # an App that GitHub brings back, or one made before it could
    web.set_up()
    page = web.client.get("/github").get_data(as_text=True)
    install = re.search(
        r'<a class="button" href="(https://github\.com/[^"]+)"([^>]*)>Install it on your repositories</a>', page
    )
    assert install and install.group(1) == APP.install_url
    back = "When the App is installed, GitHub brings you back here to choose your projects."
    tab = (
        "GitHub opens in a new tab. When the App is installed there, "
        "come back to this tab and press the button below."
    )
    if returns:
        assert "target=" not in install.group(2) and 'rel="noopener"' not in page  # the same tab: GitHub comes back
        assert f'<p class="hint" id="install-back">{back}</p>' in page and tab not in page
    else:
        assert ' target="_blank" rel="noopener"' in install.group(2)  # another tab: this one waits
        assert f'<p class="hint" id="install-back">{tab}</p>' in page and back not in page
    assert 'aria-describedby="install-back"' in install.group(2)  # the line is the button's description
    # the "?" stays after the button, and the next step is a button after the line, inside the card
    assert re.search(
        r'Install it on your repositories</a><button type="button" class="help" popovertarget="help-github-install"',
        page,
    )
    next_step = '<a class="button secondary" href="/github/installed">Next: choose your projects</a>'
    card = page.split('<section class="card readable github-card">')[1].split("</section>")[0]
    assert card.index("Install it on your repositories") < card.index('id="install-back"') < card.index(next_step)
    assert 'class="next-step"' not in page and 'href="/projects">Next' not in page  # the small link is gone
    assert page.count("Next: choose your projects") == 1 and "help-github-install" in page
    # the steps bar is the same
    assert '<li class="is-done">Create' in page and '<li aria-current="step">Install</li>' in page
    assert "<li>Choose projects</li>" in page
    assert help_buttons_inside_a_paragraph(page) == [] and " style=" not in page and "<script" not in page
    spanish = web.client.get("/github", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert ">Siguiente: elige tus proyectos</a>" in spanish
    if returns:
        assert "Cuando la App esté instalada, GitHub te trae de vuelta aquí para que elijas tus proyectos." in spanish
    else:
        assert (
            "GitHub se abre en una pestaña nueva. Cuando la App esté instalada allí, vuelve a esta pestaña" in spanish
        )
        assert "y pulsa el botón de abajo." in spanish
    # without an App the page has none of this
    none = Web(tmp_path / "none")
    none.set_up()
    assert "install-back" not in none.client.get("/github").get_data(as_text=True)


def test_add_many_adds_what_was_ticked_with_the_name_that_the_suggestion_has(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    web.logs.clear()
    response = add_many(web, "selected", "acme/web", "ACME/WORKER", "acme/web")  # any case, and twice
    assert response.status_code == 302 and response.headers["Location"] == "/projects?done=added_many&count=2"
    assert added(web) == {"acme/web", "acme/Worker"}  # as the suggestion writes them
    assert [p.weight for p in web.store.projects()] == [1, 1] and all(p.active for p in web.store.projects())
    assert [line for line in web.logs if "projects added" in line] == [
        "Web UI: projects added from the suggestions (selected): 2"
    ]
    page = web.client.get("/projects?done=added_many&count=2").get_data(as_text=True)
    assert "2 projects added. lastcall audits them in coming bursts." in page
    assert 'value="acme/web"' not in page and 'value="acme/api"' in page  # they left the suggestions
    spanish = web.client.get("/projects?done=added_many&count=2", headers={"Accept-Language": "es"})
    assert "2 proyectos añadidos. lastcall los audita en las próximas ráfagas." in spanish.get_data(as_text=True)
    one = add_many(web, "selected", "acme/docs")
    assert one.headers["Location"] == "/projects?done=added_many&count=1"
    assert "1 project added. lastcall audits it in a coming burst." in web.client.get(
        one.headers["Location"]
    ).get_data(as_text=True)
    assert "1 proyecto añadido. lastcall lo audita en una de las próximas ráfagas." in web.client.get(
        one.headers["Location"], headers={"Accept-Language": "es"}
    ).get_data(as_text=True)


@pytest.mark.parametrize("count", ["0", "-3", "abc", "", "99999999999", "1.5"])
def test_a_count_that_is_not_a_count_says_nothing(tmp_path: Path, count: str) -> None:
    web = many_web(tmp_path)
    page = web.client.get(f"/projects?done=added_many&count={count}").get_data(as_text=True)
    assert 'role="status"' not in page  # no notice, and no number that anyone wrote in the address


def test_add_many_does_nothing_with_a_value_that_is_not_a_suggestion_or_is_a_project_already(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    web.store.add_project("acme/api")
    web.store.update_project("acme/api", active=True, weight=3, language="Spanish", max_prs=2)
    web.store.add_project("acme/Cased")  # a project the suggestions also list, written in another case
    web.store.save_suggestions([*web.store.suggestions(), Suggestion("acme/cased", 50, True, None, 100, ())], NOW)
    strangers = ("acme/nothing", "evil/repo", "../etc/passwd", "acme/api ", "", "acme/docs/extra")
    response = add_many(web, "selected", "acme/api", "ACME/API", "acme/cased", *strangers, "acme/DOCS")
    assert response.headers["Location"] == "/projects?done=added_many&count=1"
    assert added(web) == {"acme/api", "acme/Cased", "acme/docs"}  # only acme/docs, as it is in the suggestions
    api = web.store.project("acme/api")
    assert api is not None and (api.weight, api.language, api.max_prs) == (3, "Spanish", 2)  # its settings stay
    # nothing that counts: the same error as with nothing ticked, and nothing added
    before = added(web)
    for repos in ((), ("acme/api",), ("acme/nothing", "evil/repo"), ("acme/docs",)):
        refused = add_many(web, "selected", *repos)
        page = refused.get_data(as_text=True)
        assert refused.status_code == 400
        assert 'role="alert"' in page and "Tick at least one repository." in page
        assert added(web) == before
    assert add_many(web, "selected").status_code == 400
    spanish = web.client.post(
        "/projects/add-many", data={"csrf": web.csrf(), "pick": "selected"}, headers={**SITE, "Accept-Language": "es"}
    )
    assert "Marca al menos un repositorio." in spanish.get_data(as_text=True)


def test_add_many_gives_the_projects_page_back_with_its_filter_when_nothing_was_chosen(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    page = add_many(web, "selected", q=" API ").get_data(as_text=True)
    assert "Tick at least one repository" in page
    assert '<input type="hidden" name="q" value="API">' in page and "3 of 7 repositories match" in page
    assert '<input id="q" name="q" type="search" value="API"' in page  # the owner sees the list as it was
    assert added(web) == set()


@pytest.mark.parametrize("pick", ["", "everything", "SELECTED"])
def test_a_button_that_the_form_does_not_have_adds_only_what_was_ticked(tmp_path: Path, pick: str) -> None:
    web = many_web(tmp_path)
    assert add_many(web, pick).status_code == 400 and added(web) == set()
    assert add_many(web, pick, "acme/docs").headers["Location"] == "/projects?done=added_many&count=1"
    assert added(web) == {"acme/docs"}


def test_add_many_adds_the_suggested_ones_or_all_of_them_as_the_filter_shows_them(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    web.store.add_project("acme/web")  # not a suggestion any more
    # the suggested ones left: api, Worker and api-client; the tick of another one does not count
    done = add_many(web, "suggested", "acme/docs", q="")
    assert done.headers["Location"] == "/projects?done=added_many&count=3"
    assert added(web) == {"acme/web", "acme/api", "acme/Worker", "acme/api-client"}
    # all of what is left: docs, old-site and legacy-API
    assert add_many(web, "all").headers["Location"] == "/projects?done=added_many&count=3"
    assert added(web) == {"acme/web", *SUGGESTED, *OTHERS}
    assert [line for line in web.logs if "projects added" in line][-2:] == [
        "Web UI: projects added from the suggestions (suggested): 3",
        "Web UI: projects added from the suggestions (all): 3",
    ]
    assert (
        add_many(web, "all").status_code == 400 and add_many(web, "suggested").status_code == 400
    )  # nothing is left


def test_add_all_skips_the_archived_repositories_the_forks_and_the_empty_ones_which_only_a_tick_adds(
    tmp_path: Path,
) -> None:
    web = many_web(tmp_path, suggested=("acme/api",), others=("acme/docs",))
    left_out = [Suggestion(f"acme/{why}", 0, False, None, 0, ((why, {}),)) for why in ("archived", "fork", "empty")]
    web.store.save_suggestions([*web.store.suggestions(), *left_out], NOW)
    form = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert ">Add all 2</button>" in form  # api and docs, not the three that lastcall leaves out
    assert "“Add all” leaves out the archived repositories, the forks and the empty ones" in form  # and says so
    assert 'value="acme/archived"' in form and 'value="acme/fork"' in form  # they can still be ticked
    assert add_many(web, "all").headers["Location"] == "/projects?done=added_many&count=2"
    assert added(web) == {"acme/api", "acme/docs"}
    form = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert 'value="all"' not in form and 'value="suggested"' not in form  # nothing left that Add all would add
    assert add_many(web, "all").status_code == 400
    assert add_many(web, "selected", "acme/fork").headers["Location"] == "/projects?done=added_many&count=1"
    assert added(web) == {"acme/api", "acme/docs", "acme/fork"}


def test_add_many_with_a_filter_adds_what_the_owner_sees(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    # "api", whatever the case: api and api-client are suggested, legacy-API is not
    assert add_many(web, "suggested", q="API").headers["Location"] == "/projects?done=added_many&count=2"
    assert added(web) == {"acme/api", "acme/api-client"}
    assert add_many(web, "all", q="  api ").headers["Location"] == "/projects?done=added_many&count=1"
    assert added(web) == {"acme/api", "acme/api-client", "acme/legacy-API"}
    assert add_many(web, "all", q="api").status_code == 400  # all that it shows is added
    assert add_many(web, "suggested", q="zzz").status_code == 400
    assert add_many(web, "all", q="zzz").status_code == 400
    # what the owner ticked counts whatever the filter says: the ticked rows were on the page
    assert add_many(web, "selected", "acme/docs", q="api").headers["Location"] == "/projects?done=added_many&count=1"
    assert added(web) == {"acme/api", "acme/api-client", "acme/legacy-API", "acme/docs"}
    # a filter cut at 100 characters, as the page cuts it
    assert add_many(web, "all", q="x" * 5000).status_code == 400


def test_add_many_needs_the_csrf_token_this_sites_origin_and_a_signed_in_owner(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    form = {"pick": "all", "repo": ["acme/docs"]}
    missing = web.client.post("/projects/add-many", data=form, headers=SITE)
    forged = web.client.post("/projects/add-many", data={**form, "csrf": "forged"}, headers=SITE)
    for refused in (missing, forged):
        assert refused.status_code == 403 and "This form has expired" in refused.get_data(as_text=True)
    elsewhere = web.client.post(
        "/projects/add-many", data={**form, "csrf": web.csrf()}, headers={"Origin": "https://evil.example"}
    )
    assert elsewhere.status_code == 403 and "Form not accepted" in elsewhere.get_data(as_text=True)
    stranger = web.app.test_client()
    signed_out = stranger.post("/projects/add-many", data={**form, "csrf": web.csrf()}, headers=SITE)
    assert signed_out.status_code == 302 and signed_out.headers["Location"] == "/login"
    assert added(web) == set()  # and no GitHub was asked: NoGitHub would have failed


def test_the_projects_page_has_one_form_for_the_suggestions_with_a_checkbox_in_each_row(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    page = web.client.get("/projects").get_data(as_text=True)
    form = pick_form(page)
    assert re.search(
        r'<input type="hidden" name="csrf" value="[^"]+">\s*<input type="hidden" name="q" value="">', form
    )
    assert re.search(r'<fieldset class="picks">\s*<legend>Choose the repositories to add</legend>', form)
    assert re.findall(r'type="checkbox" name="repo" value="([^"]+)"', form) == [*SUGGESTED, *OTHERS]  # best first
    for name in (*SUGGESTED, *OTHERS):
        box = "pick-" + name.replace("/", "--")
        assert f'<input id="{box}" type="checkbox" name="repo" value="{name}">' in form
        assert f'<label for="{box}" class="suggestion-name">{name}</label>' in form  # the name ticks the box
        assert f'<a class="repo-link" href="https://github.com/{name}"' in form  # and the link is a separate one
    assert form.count('class="badge">suggested</span>') == 4 and form.count("<li ") == 7
    assert form.count('class="chip score"') == 7 and "last push 3\u00a0days ago" in form  # the rest of each row stays
    assert re.search(r'class="help" popovertarget="help-projects-score"', form)
    # the rows have no form of their own: the only other form that posts to /projects/add takes an address
    assert page.count('action="/projects/add"') == 1
    assert 'for="repo">owner/name, or its address on GitHub</label>' in page
    # three buttons: the two that add many at once above the list, and the primary one in the bar under it; the "?"
    # inside the form never submits
    buttons = re.findall(r"<button[^>]*>[^<]*</button>", form)
    submits = [b for b in buttons if 'name="pick"' in b]
    assert submits == [
        '<button class="secondary" name="pick" value="suggested">Add the 4 suggested</button>',
        '<button class="secondary" name="pick" value="all">Add all 7</button>',
        '<button name="pick" value="selected">Add selected</button>',
    ]
    assert form.index('class="pick-all"') < form.index('<fieldset class="picks">') < form.index('class="pick-bar"')
    bar = form.split('<div class="pick-bar">')[1]
    assert bar.count('name="pick"') == 1 and 'value="selected"' in bar  # the bar at the bottom stays small
    assert all(b.startswith('<button type="button" class="help"') for b in buttons if b not in submits)
    assert help_buttons_inside_a_paragraph(page) == [] and " style=" not in page and "<script" not in page
    assert not re.search(r"\son[a-z]+=", page)
    spanish = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert 'value="selected">Añadir los marcados</button>' in spanish
    assert ">Añadir los 4 sugeridos</button>" in spanish and ">Añadir todos (7)</button>" in spanish
    assert "<legend>Elige los repositorios que quieres añadir</legend>" in spanish


def test_the_buttons_that_add_many_count_what_is_shown_and_appear_only_when_they_mean_something(
    tmp_path: Path,
) -> None:
    web = many_web(tmp_path, suggested=("acme/api",), others=())
    page = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert ">Add selected</button>" in page and ">Add the suggested one</button>" in page
    assert 'value="all"' not in page  # nothing besides the suggested ones: no "Add all"
    suggest_many(web.store, suggested=(), others=("acme/docs",))
    page = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert ">Add selected</button>" in page and ">Add the repository</button>" in page
    assert 'value="suggested"' not in page and "<details" in page  # nothing recommended: no such button
    suggest_many(web.store, suggested=("acme/api", "acme/web"), others=("acme/docs", "acme/old-site", "acme/x"))
    page = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert ">Add the 2 suggested</button>" in page and ">Add all 5</button>" in page
    assert "<summary>3 more repositories</summary>" in page
    web.store.add_project("acme/web")  # a project is not offered any more
    page = pick_form(web.client.get("/projects").get_data(as_text=True))
    assert ">Add the suggested one</button>" in page and ">Add all 4</button>" in page
    empty = web.client.get("/projects?q=nothing-like-it").get_data(as_text=True)
    assert 'name="pick"' not in empty and "/projects/add-many" not in empty  # no list, no buttons
    web.store.save_suggestions([], NOW)
    none = web.client.get("/projects").get_data(as_text=True)
    assert "/projects/add-many" not in none and 'type="search"' not in none


def test_the_projects_page_filters_the_suggestions_by_name_whatever_the_case(tmp_path: Path) -> None:
    web = many_web(tmp_path)  # seven suggestions: more than six
    page = web.client.get("/projects").get_data(as_text=True)
    assert '<div id="suggestions">' in page  # where the filter and Show all bring the page back to
    assert '<form method="get" action="/projects#suggestions" class="filter" role="search">' in page
    assert '<label for="q">Filter by name</label>' in page and 'type="search" value=""' in page
    assert ">Filter</button>" in page and 'class="filter-note"' not in page and "<details>" in page
    page = web.client.get("/projects?q=API").get_data(as_text=True)
    form = pick_form(page)
    assert re.findall(r'name="repo" value="([^"]+)"', form) == ["acme/api", "acme/api-client", "acme/legacy-API"]
    assert (
        '<p class="filter-note">3 of 7 repositories match “API”. <a href="/projects#suggestions">Show all</a></p>'
        in page
    )
    assert '<input type="hidden" name="q" value="API">' in form and 'type="search" value="API"' in page
    assert ">Add the 2 suggested</button>" in form and ">Add all 3</button>" in form  # what is shown
    assert "<details open>" in form and "<summary>1 more repository</summary>" in form  # a match is never hidden
    spanish = web.client.get("/projects?q=api", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert "3 de 7 repositorios coinciden con «api». " in spanish and ">Ver todos</a>" in spanish
    assert ">Filtrar</button>" in spanish and "Filtrar por nombre</label>" in spanish
    nothing = web.client.get("/projects?q=zzz").get_data(as_text=True)
    assert "0 of 7 repositories match “zzz”." in nothing and 'type="search" value="zzz"' in nothing
    assert 'type="checkbox"' not in nothing and 'name="pick"' not in nothing  # the filter stays, to be cleared
    assert '<a href="/projects#suggestions">Show all</a>' in nothing
    # the text is the owner's own: it is escaped, spaces around it do not count, and a long one is cut
    odd = web.client.get("/projects?q=%3Cb%3Ex%3C/b%3E").get_data(as_text=True)
    assert "&lt;b&gt;x&lt;/b&gt;" in odd and "<b>x</b>" not in odd
    spaced = web.client.get("/projects?q=%20%20api%20").get_data(as_text=True)
    assert "3 of 7 repositories match “api”." in spaced
    assert f'value="{"x" * 100}"' in web.client.get("/projects?q=" + "x" * 300).get_data(as_text=True)
    assert " style=" not in nothing and "<script" not in nothing and help_buttons_inside_a_paragraph(nothing) == []


def test_the_filter_shows_with_more_than_six_suggestions_or_when_one_is_set(tmp_path: Path) -> None:
    web = many_web(tmp_path, suggested=("acme/a", "acme/b", "acme/c"), others=("acme/d", "acme/e", "acme/f"))
    few = web.client.get("/projects").get_data(as_text=True)
    assert 'type="search"' not in few and "filter-note" not in few  # six: no filter, and no one to confuse
    assert 'type="checkbox"' in few and ">Add all 6</button>" in few
    narrowed = web.client.get("/projects?q=b").get_data(as_text=True)
    assert 'type="search" value="b"' in narrowed  # set: it shows
    assert "1 of 6 repositories match “b”." in narrowed
    # Show all lands where the filter was, even when the list is too short to need one
    assert '<div id="suggestions">' in narrowed and '<a href="/projects#suggestions">Show all</a>' in narrowed
    assert '<div id="suggestions">' in few
    suggest_many(web.store, suggested=("acme/a", "acme/b", "acme/c"), others=("acme/d", "acme/e", "acme/f", "acme/g"))
    assert 'type="search"' in web.client.get("/projects").get_data(as_text=True)  # seven: it shows
    web.store.add_project("acme/a")  # a project is not a suggestion: six are left
    assert 'type="search"' not in web.client.get("/projects").get_data(as_text=True)
    web.store.save_suggestions([], NOW)  # nothing to filter, nothing to come back to
    assert 'id="suggestions"' not in web.client.get("/projects?q=b").get_data(as_text=True)


def test_the_projects_page_without_the_app_has_no_suggestions_to_add_many_of(web: Web) -> None:
    web.set_up()
    suggest_many(web.store)
    page = web.client.get("/projects?q=api").get_data(as_text=True)
    assert "Create the GitHub App first" in page
    assert "/projects/add-many" not in page and 'type="search"' not in page


def test_what_the_filter_and_add_many_ask_of_the_suggestions() -> None:
    from lastcall.store import ProjectRow
    from lastcall.web.project_views import clean_query, left_to_add, matching

    found = [
        Suggestion(name, 50, True, None, 0, ()) for name in ("acme/API", "acme/web", "Acme/api-client", "acme/x")
    ]
    assert [s.repo for s in matching(found, "api")] == ["acme/API", "Acme/api-client"]
    assert [s.repo for s in matching(found, "API-")] == ["Acme/api-client"]
    assert [s.repo for s in matching(found, "")] == [s.repo for s in found]
    assert matching(found, "acme/web ") == []  # clean_query trims it before
    assert clean_query("  api \n") == "api" and clean_query("x" * 500) == "x" * 100 and clean_query("   ") == ""
    projects = [ProjectRow("ACME/web", True, 1, None, 3)]
    assert [s.repo for s in left_to_add(projects, found)] == ["acme/API", "Acme/api-client", "acme/x"]


def test_the_layout_of_the_add_many_form_keeps_its_buttons_in_view() -> None:
    css = (Path(__file__).parent.parent / "src/lastcall/web/static/style.css").read_text(encoding="utf-8")
    bar = re.search(r"\n\.pick-bar \{(.*?)\n\}", css, re.S)
    assert bar is not None and "position: sticky;" in bar.group(1) and "bottom: 0;" in bar.group(1)
    assert "html:has(.pick-bar) { scroll-padding-bottom:" in css  # a field in focus is never behind the bar
    assert '.suggestion-name::after { content: ""; position: absolute; inset: 0; }' in css  # a tap anywhere ticks
    assert (
        "button.secondary, a.button.secondary {" in css
    )  # "Next: choose your projects" is a link in a button's look


# the state of a card is a button, and the trash button removes a project after asking


def head(page: str, repo: str) -> str:
    """The header of a project's card: its name, its state with its "?", and its trash button."""
    return card(page, repo).split("</header>")[0]


def question(page: str, number: int) -> str:
    """What the trash button of the `number`-th card opens: the popover that asks before a project is removed."""
    found = re.search(rf'<div id="remove-{number}" popover class="bubble confirm"[^>]*>(.*?)</div>', page, re.S)
    assert found, number
    return found.group(1)


def test_the_state_of_a_card_is_a_button_in_a_small_form_that_says_what_a_press_does(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")  # on, with priority 1
    web.store.add_project("acme/web", active=False)
    csrf = web.csrf()
    page = web.client.get("/projects").get_data(as_text=True)
    on, off = head(page, "acme/api"), head(page, "acme/web")
    # a form with the CSRF field and the state that a press asks for
    assert '<form method="post" action="/projects/acme/api/active" class="state-form">' in on
    assert f'<input type="hidden" name="csrf" value="{csrf}">' in on and f'name="csrf" value="{csrf}"' in off
    assert '<input type="hidden" name="active" value="0">' in on
    assert '<input type="hidden" name="active" value="1">' in off
    # it keeps its look and its text; its name says what a press does, and keeps the text that the chip shows
    state = "active · priority\u00a0\u00d71"
    assert (
        f'<button class="chip state-active" aria-label="Turn off bursts on acme/api ({state})">{state}</button>' in on
    )
    assert (
        '<button class="chip state-off" aria-label="Turn on bursts on acme/web (inactive)">inactive</button>' in off
    )
    # its "?" (one for the page: the first card has it) comes right after it, and says what a click does
    assert re.search(r'</form>\s*<button type="button" class="help" popovertarget="help-projects-active"', on)
    assert 'popovertarget="help-projects-active"' not in off
    assert "Click the state at the top of a card to turn bursts on or off; the “Active” box in Settings" in page
    assert "aria-pressed" not in page  # its name changes with its state, and a toggle's name must not
    # the Active box in Settings stays, as it was
    assert '<input type="checkbox" aria-describedby="active-hint-1" name="active" value="1" checked>' in page
    assert '<input type="checkbox" aria-describedby="active-hint-2" name="active" value="1">' in page


def test_a_press_on_the_state_turns_bursts_on_or_off_and_returns_to_the_card_with_no_notice(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.update_project(
        "acme/api",
        active=True,
        weight=2,
        language="Spanish",
        max_prs=1,
        merge_on_approve=True,
        watch=True,
        health_url="https://example.com/health",
        tests=True,
        deps=True,
        signals="shop",
    )
    settings = web.store.project("acme/api")
    assert settings is not None
    csrf = web.csrf()

    def press(active: str) -> TestResponse:
        return web.client.post("/projects/acme/api/active", data={"csrf": csrf, "active": active}, headers=SITE)

    response = press("0")
    assert response.status_code == 302 and response.headers["Location"] == "/projects#project-acme--api"
    assert web.store.project("acme/api") == replace(settings, active=False)  # the flag, and nothing else
    page = web.client.get(response.headers["Location"]).get_data(as_text=True)
    assert 'role="status"' not in page and 'class="card project is-off" id="project-acme--api"' in page
    assert ">inactive</button>" in page and 'aria-label="Turn on bursts on acme/api (inactive)"' in page
    assert press("1").headers["Location"] == "/projects#project-acme--api"
    assert web.store.project("acme/api") == settings
    assert "priority\u00a0\u00d72</button>" in web.client.get("/projects").get_data(as_text=True)
    # the form says the state that it wants: a page that is out of date cannot flip the project the wrong way
    press("1")
    assert web.store.project("acme/api") == settings
    press("0")
    press("0")
    assert web.store.project("acme/api") == replace(settings, active=False)
    press("1")
    press("")  # a form that says nothing wants it off
    assert web.store.project("acme/api") == replace(settings, active=False)
    # the Active box in Settings goes on working
    form = {"csrf": csrf, "weight": "2", "language": "Spanish", "max_prs": "1", "active": "1"}
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    project = web.store.project("acme/api")
    assert project is not None and project.active and project.weight == 2
    web.client.post("/projects/acme/api", data={k: v for k, v in form.items() if k != "active"}, headers=SITE)
    project = web.store.project("acme/api")
    assert project is not None and not project.active


def test_turning_on_a_project_that_has_no_priority_gives_it_weight_1_or_it_would_stay_off(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.update_project("acme/api", active=True, weight=0, language=None, max_prs=3)  # `project add --weight 0`
    csrf = web.csrf()

    def press(active: str) -> None:
        web.client.post("/projects/acme/api/active", data={"csrf": csrf, "active": active}, headers=SITE)

    page = web.client.get("/projects").get_data(as_text=True)
    assert 'aria-label="Turn on bursts on acme/api (inactive)"' in page  # active, and yet off
    press("0")
    project = web.store.project("acme/api")
    assert project is not None and (project.active, project.weight) == (False, 0)  # turning off changes the flag only
    press("1")
    project = web.store.project("acme/api")
    assert project is not None and (project.active, project.weight) == (True, 1)
    page = web.client.get("/projects").get_data(as_text=True)
    assert 'aria-label="Turn off bursts on acme/api (active · priority\u00a0\u00d71)"' in page
    web.store.update_project("acme/api", active=False, weight=0.5, language=None, max_prs=3)
    press("1")
    project = web.store.project("acme/api")
    assert project is not None and (project.active, project.weight) == (True, 0.5)  # a priority of 0.5 stays


def test_the_state_and_the_trash_button_need_the_csrf_token_this_sites_origin_and_a_signed_in_owner(
    tmp_path: Path,
) -> None:
    web = Web(tmp_path / "state-and-trash", github=FakeGitHub())
    web.set_up()
    web.store.add_project("acme/api")
    web.store.add_order("audit", "acme/api", None, NOW)
    before = web.store.project("acme/api")
    for action in ("active", "remove"):
        url, form = f"/projects/acme/api/{action}", {"active": "0"}
        missing = web.client.post(url, data=form, headers=SITE)
        forged = web.client.post(url, data={**form, "csrf": "forged"}, headers=SITE)
        for refused in (missing, forged):
            assert refused.status_code == 403 and "This form has expired" in refused.get_data(as_text=True)
        elsewhere = web.client.post(
            url, data={**form, "csrf": web.csrf()}, headers={"Origin": "https://evil.example"}
        )
        assert elsewhere.status_code == 403 and "Form not accepted" in elsewhere.get_data(as_text=True)
        stranger = web.app.test_client()
        signed_out = stranger.post(url, data={**form, "csrf": web.csrf()}, headers=SITE)
        assert signed_out.status_code == 302 and signed_out.headers["Location"] == "/login"
        assert web.client.get(url).status_code == 405  # a link cannot do it
        nothing = web.client.post(f"/projects/acme/nothing/{action}", data={**form, "csrf": web.csrf()}, headers=SITE)
        assert nothing.status_code == 404 and "This page does not exist" in nothing.get_data(as_text=True)
    assert web.store.project("acme/api") == before and len(web.store.orders()) == 1  # nothing happened
    fresh = Web(tmp_path / "fresh")  # no password yet: the owner signs up first
    for action in ("active", "remove"):
        sign_up = fresh.client.post(f"/projects/acme/api/{action}", data={"csrf": "x"}, headers=SITE)
        assert sign_up.status_code == 302 and sign_up.headers["Location"] == "/setup"


def test_the_state_and_the_trash_button_have_a_name_in_english_and_in_spanish(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_project("acme/web", active=False)
    english = web.client.get("/projects").get_data(as_text=True)
    spanish = web.client.get("/projects", headers={"Accept-Language": "es"}).get_data(as_text=True)
    names = {
        "en": (
            "Turn off bursts on acme/api (active · priority\u00a0\u00d71)",
            "Turn on bursts on acme/web (inactive)",
            "Remove acme/api from lastcall",
            "Remove acme/web from lastcall",
        ),
        "es": (
            "Desactivar las ráfagas en acme/api (activo · prioridad\u00a0\u00d71)",
            "Activar las ráfagas en acme/web (inactivo)",
            "Quitar acme/api de lastcall",
            "Quitar acme/web de lastcall",
        ),
    }
    for lang, page in (("en", english), ("es", spanish)):
        for name in names[lang]:
            assert f'aria-label="{name}"' in page, name
    assert "Haz clic en el estado, arriba en la tarjeta, para activar o desactivar las ráfagas" in spanish
    assert '<h3 id="remove-title-1">¿Quitar acme/api de lastcall?</h3>' in spanish
    assert '<button class="danger">Quitar</button>' in spanish and ">Cancelar</button>" in spanish


def test_the_trash_button_only_opens_a_question_whose_form_holds_the_removal(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_project("acme/web")
    csrf = web.csrf()
    page = web.client.get("/projects").get_data(as_text=True)
    # it is no submit button and it is in no form of its own: it opens its popover and removes nothing by itself
    top = head(page, "acme/api")
    trash = re.search(
        r'<button type="button" class="trash" popovertarget="remove-1" aria-label="Remove acme/api from lastcall" '
        r'title="Remove acme/api from lastcall"><svg [^>]*aria-hidden="true"[^>]*><path d="[^"]+"/></svg></button>',
        top,
    )
    assert trash and top.index("</form>") < trash.start()
    assert top.count("<form") == 1 == head(page, "acme/web").count("<form")  # only the state's own
    asked = question(page, 1)
    assert '<h3 id="remove-title-1">Remove acme/api from lastcall?</h3>' in asked
    for line in (
        "lastcall stops working on this repository.",
        "Its history and what lastcall learned stay. Add it again and it picks up where it left off, without "
        "proposing again what you rejected.",
        "Its settings (priority, language and the rest) are forgotten.",
        "Nothing changes on GitHub: the repository and its pull requests stay.",
        "It goes back to the list of repositories to add.",
    ):
        assert f"<li>{line}</li>" in asked
    assert '<form method="post" action="/projects/acme/api/remove" class="form-actions">' in asked
    assert f'<input type="hidden" name="csrf" value="{csrf}">' in asked
    cancel = '<button type="button" class="secondary" popovertarget="remove-1" popovertargetaction="hide" autofocus>'
    assert f"{cancel}Cancel</button>" in asked  # it closes the popover, and has the focus when it opens
    assert '<button class="danger">Remove</button>' in asked  # the only button that submits
    assert asked.count("<button") == 2
    # each card asks about its own project, and the removal is posted from there and from nowhere else
    assert page.count('action="/projects/acme/api/remove"') == 1
    assert 'action="/projects/acme/web/remove"' in question(page, 2)
    assert 'popovertarget="remove-2"' in head(page, "acme/web")
    # it reads as a dialog with its title and what it says, and it takes no part in the help bubbles' bookkeeping
    assert 'role="dialog" aria-labelledby="remove-title-1" aria-describedby="remove-what-1"' in page
    targets = set(re.findall(r'popovertarget="(help-[^"]+)"(?! popovertargetaction)', page))
    assert sorted(re.findall(r'id="(help-[^"]+)" popover', page)) == sorted(targets)
    assert help_buttons_inside_a_paragraph(page) == []
    # nothing that the content security policy blocks
    assert "<script" not in page and "<style" not in page
    assert "<img" not in card(page, "acme/api") and not re.search(r"\s(style|on[a-z]+)=", card(page, "acme/api"))


def test_removing_a_project_keeps_its_history_and_it_returns_to_the_suggestions(tmp_path: Path) -> None:
    web = many_web(tmp_path)
    assert add_many(web, "selected", "acme/api", "acme/web").status_code == 302
    web.store.add_account("claude", "claude-code")
    report = AuditReport("acme/api", "abc1234", "claude-code", "English")
    finding = {"title": "T", "severity": "high", "file": "a.py", "lines": "1", "why": "w", "fix": "f"}
    report.findings = [{**finding, "id": f"B{n}"} for n in (1, 2, 3)]
    report.verdicts = [{"id": f"B{n}", "verdict": "valid", "decision": "pr"} for n in (1, 2, 3)]
    report.deliveries = [
        {"status": "opened", "url": "https://github.com/acme/api/pull/7", "title": "Fix B1", "ids": ["B1"]},
        {"status": "opened", "url": "https://github.com/acme/api/pull/8", "title": "Fix B2", "ids": ["B2"]},
    ]
    web.store.start_run("api-1", "acme/api", "claude", "burst")
    web.store.finish_run("api-1", "ok", "abc1234")
    web.store.record_findings("api-1", report)
    web.store.record_deliveries("api-1", report)
    web.store.close_pr("https://github.com/acme/api/pull/7", merged=False)  # the owner rejected one
    web.store.add_order("audit", "acme/api", None, NOW)
    web.store.add_order("audit", "acme/web", None, NOW)
    earlier = web.store.earlier("acme/api")
    assert {e["state"] for e in earlier} == {"rejected", "in_pr", "pending"}
    web.logs.clear()
    response = web.client.post("/projects/acme/api/remove", data={"csrf": web.csrf()}, headers=SITE)
    assert response.status_code == 302 and response.headers["Location"] == "/projects?done=removed&repo=acme/api"
    assert web.logs == ["Web UI: project acme/api removed"]  # one line
    assert added(web) == {"acme/web"} and [o["repo"] for o in web.store.orders()] == ["acme/web"]
    assert web.store.earlier("acme/api") == earlier and [r["id"] for r in web.store.runs()] == ["api-1"]
    page = web.client.get(response.headers["Location"]).get_data(as_text=True)
    assert (
        '<p class="notice done" role="status">acme/api removed. Its history stays: add it again from the list below '
        "at any time.</p>" in page
    )
    assert 'id="project-acme--api"' not in page and 'id="project-acme--web"' in page
    assert 'id="pick-acme--api"' in pick_form(page)  # back in the list of repositories to add
    spanish = web.client.get(response.headers["Location"], headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert (
        "acme/api quitado. Su historial se queda: vuelve a añadirlo cuando quieras desde la lista de abajo."
        in spanish
    )
    for path in ("/", "/projects", "/accounts", "/settings", "/github"):  # nothing breaks with the project gone
        assert web.client.get(path).status_code == 200, path
    dashboard = web.client.get("/").get_data(as_text=True)
    assert "acme/api#8" in dashboard  # its open pull request still waits for the owner
    # adding it again picks up where it left off, and the judge still knows what the owner rejected
    assert add_many(web, "selected", "acme/api").status_code == 302
    assert added(web) == {"acme/api", "acme/web"} and web.store.earlier("acme/api") == earlier
    page = web.client.get("/projects").get_data(as_text=True)
    assert "1 in review · 1 pending · 1 rejected" in card(page, "acme/api")


def test_a_project_with_a_run_going_is_not_removed_and_the_page_says_why(github_web: Web) -> None:
    web = github_web
    web.store.add_project("acme/api")
    web.store.add_account("claude", "claude-code")
    web.store.add_order("audit", "acme/api", None, NOW)
    web.store.start_run("api-1", "acme/api", "claude", "manual")
    csrf = web.csrf()
    web.logs.clear()
    response = web.client.post("/projects/acme/api/remove", data={"csrf": csrf}, headers=SITE)
    page = response.get_data(as_text=True)
    assert response.status_code == 409 and 'class="notice error" role="alert"' in page
    assert "<p>A run is going on acme/api right now. Remove it when the run ends.</p>" in page
    assert 'id="project-acme--api"' in page and 'role="status"' not in page  # the Projects page, with its card
    assert web.store.project("acme/api") is not None and len(web.store.orders()) == 1 and web.logs == []
    spanish = web.client.post(
        "/projects/acme/api/remove", data={"csrf": csrf}, headers={**SITE, "Accept-Language": "es"}
    ).get_data(as_text=True)
    assert "Hay una ejecución en curso en acme/api. Quítalo cuando termine." in spanish
    web.store.finish_run("api-1", "ok", None)  # the run ends, and the same button removes it
    assert web.client.post("/projects/acme/api/remove", data={"csrf": csrf}, headers=SITE).status_code == 302
    assert web.store.project("acme/api") is None and web.store.orders() == []


@pytest.mark.parametrize(
    "repo", ["", "x", "acme/api <b>", "../etc/passwd", "acme/api/extra", "a" * 300, "https://e.x/y"]
)
def test_a_removal_notice_names_only_what_looks_like_a_repository(github_web: Web, repo: str) -> None:
    page = github_web.client.get("/projects", query_string={"done": "removed", "repo": repo}).get_data(as_text=True)
    assert 'role="status"' not in page  # no notice, and no text that anyone wrote in the address


def test_a_removed_project_keeps_its_container_and_source_choices_and_settings_never_erases_them(
    tmp_path: Path,
) -> None:
    from lastcall import logs, loki

    listed = [{"name": "shop-web-1", "image": "ghcr.io/acme/shop:1.2", "state": "running", "compose_project": "shop"}]
    fake = FakeLoki([{"container": "shop-web-1", "compose_project": "shop", "host": "vps-1"}])
    web = Web(tmp_path, containers=lambda: listed, loki_fetch=fake)
    web.set_up()
    loki.Loki("http://loki.example.com:3100").save(web.secrets)
    web.store.add_project("acme/shop")
    web.store.add_project("acme/blog")
    logs.choose(web.store, {"shop-web-1": "acme/shop"})
    web.store.set_setting(loki.LABEL, "container")
    loki.choose(web.store, {"shop-web-1": "acme/shop"})
    csrf = web.csrf()
    assert web.client.post("/projects/acme/shop/remove", data={"csrf": csrf}, headers=SITE).status_code == 302
    page = web.client.get("/settings").get_data(as_text=True)
    note = "It was chosen for acme/shop, which is not a project now: add acme/shop again and it is read again."
    assert page.count(note) == 2  # a container and a source
    assert '<option value="acme/blog" selected>' not in page and 'value="acme/shop"' not in page
    assert page.count('<input type="hidden" name="shown-1" value="">') == 2  # the page shows no project for them
    spanish = web.client.get("/settings", headers={"Accept-Language": "es"}).get_data(as_text=True)
    assert spanish.count("Se eligió para acme/shop, que ya no es un proyecto: añade acme/shop de nuevo") == 2
    # saving the form as the page sends it keeps the choices
    form = {"csrf": csrf, "name-1": "shop-web-1", "project-1": "", "shown-1": ""}
    assert web.client.post("/settings/logs", data=form, headers=SITE).status_code == 302
    sources = web.client.post("/settings/loki/sources", data={**form, "label": "container"}, headers=SITE)
    assert sources.status_code == 302
    assert logs.chosen(web.store) == {"shop-web-1": "acme/shop"} == loki.chosen(web.store)
    # adding the project again brings the choices back, with nothing to say about them
    web.store.add_project("acme/shop")
    page = web.client.get("/settings").get_data(as_text=True)
    assert page.count('<option value="acme/shop" selected>acme/shop</option>') == 2
    assert "is not a project now" not in page
    assert page.count('<input type="hidden" name="shown-1" value="acme/shop">') == 2
    assert logs.names_for("acme/shop", listed, logs.chosen(web.store)) == ["shop-web-1"]


def test_without_popover_support_neither_the_question_nor_the_trash_button_shows() -> None:
    css = (Path(__file__).parent.parent / "src/lastcall/web/static/style.css").read_text(encoding="utf-8")
    fallbacks = re.findall(r"@supports not selector\(:popover-open\) \{(.*?)\n\}", css, re.S)
    mine = next(block for block in fallbacks if ".bubble.confirm" in block)
    assert ".trash, .bubble.confirm { display: none; }" in mine  # never a Remove button one click away in a card
    # an author's display on a popover overrides the browser's, and would show the closed question
    assert not re.search(r"\.bubble\.confirm\s*\{[^}]*\bdisplay:", css.replace(mine, ""))
