import html
import http.client
import json
import re
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request

import pytest
import waitress.server
from flask.testing import FlaskClient

from lastcall.accounts import Account
from lastcall.github import App, GitHub, Response
from lastcall.notify import Notifier
from lastcall.quota import HOUR, QuotaError, Reading, Window
from lastcall.recipes.audit import AuditReport
from lastcall.secretstore import SecretStore
from lastcall.store import Store
from lastcall.tiers import TIERS
from lastcall.web import CSP, create_app, serve
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
from test_projects import FakeGitHub

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
            background=lambda work: work(),
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
    assert 'acme/api</a> <span class="badge">sugerido</span>' in page
    assert "tests · CI · 1 issue abierto" in page and "unos 11k tokens de código" in page
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
    assert 'class="status-line"' in page and 'href="/projects">Next: choose your projects</a>' in page
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
        assert "reset passed, waiting for a new reading" in page
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
    for path in ("/", "/accounts"):
        page = web.client.get(path).get_data(as_text=True)
        assert ">Needs a key</span>" in page and ">Bursting</span>" not in page, path
        assert "Burst in progress" not in page and "is-live" not in page and missing in page, path
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
