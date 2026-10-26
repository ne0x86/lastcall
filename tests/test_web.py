import html
import http.client
import json
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import waitress.server
from flask.testing import FlaskClient

from lastcall.accounts import Account
from lastcall.github import App, GitHub
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
        web = Web(tmp_path / f"trusted-{trusted}", trust_proxy=trusted)
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
    assert "Paused. No new runs start." in page and "New runs: paused." in page
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
    assert "se reinicia en 30 h" in page
    assert "Próxima ráfaga en 14 h, para gastar 28\u00a0%." in page  # 28% left at 2%/h: 16 h before the reset
    assert "Fix &lt;b&gt;it&lt;/b&gt;" in page  # titles come from agents: escaped
    assert "1 decisión en ne0x86/demo" in page and 'href="https://github.com/ne0x86/demo/issues/8"' in page
    assert "1 en revisión · 1 por decidir · 1 descartado" in page
    assert "Cuota leída hace 10 min." in page and "terminada" in page
    assert "El servicio no está en marcha" not in page

    web.client.get("/language/en")
    page = web.client.get("/").get_data(as_text=True)
    assert "Weekly window" in page and "62% used" in page and "Next burst in 14 h, to spend 28%." in page
    assert "Writes in Spanish · reserve 10%" in page and "1 in review · 1 to decide · 1 discarded" in page
    assert "Getting started" in page  # no GitHub App yet


def test_dashboard_says_when_the_service_is_not_running(web: Web) -> None:
    web.set_up()
    page = web.client.get("/").get_data(as_text=True)
    assert "The service is not running" in page and "Last seen" not in page
    web.store.set_setting("heartbeat", str(NOW - 3 * HOUR))
    assert "Last seen 3 h ago." in web.client.get("/").get_data(as_text=True)
    web.store.set_setting("heartbeat", str(NOW - 60))
    assert "The service is not running" not in web.client.get("/").get_data(as_text=True)


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
        assert response.headers["Location"] == "/projects?done=queued"
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
    assert added.headers["Location"] == "/settings?done=notify_added"
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
    assert sent.headers["Location"] == "/settings?done=notify_sent"
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
    header = web.client.get("/accounts").get_data(as_text=True)
    assert 'hreflang="es">Español</a>' in header  # the page's own lists never replace the header's

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
        assert response.headers["Location"] == "/accounts?done=reading"
    assert [a.id for a in readings] == ["max", "broken"]
    assert web.store.last_reading("max") is not None
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "The last reading failed: not signed in" in page and "12% used" in page

    form = {"csrf": csrf, "language": "Spanish", "reserve": "15"}
    assert web.client.post("/accounts/max", data=form, headers=SITE).status_code == 302
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
    page = web.client.get("/accounts").get_data(as_text=True)
    assert "On OpenRouter · model qwen/qwen3-coder · writes in English" in page
    assert "Budget this month" in page and "$5.00 of $20.00" in page
    assert "Burst in progress: $5.00 of $20.00 spent this month, behind the calendar." in page
    assert "Local models cost nothing." in page and "at the hours of its schedule (* 1-6 * * *, UTC)" in page
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
    assert "Read the quota now" not in page
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
    assert response.headers["Location"] == "/projects?done=improve"
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
    assert 'name="tests" value="1"> Tests too: in turn with the audits, bursts on this project write tests' in page
    csrf = web.csrf()
    form = {"csrf": csrf, "weight": "1", "language": "", "max_prs": "3", "active": "1", "tests": "1"}
    assert web.client.post("/projects/acme/api", data=form, headers=SITE).status_code == 302
    assert web.store.projects()[0].tests
    response = web.client.post("/projects/acme/api/tests", data={"csrf": csrf, "account": "claude"}, headers=SITE)
    assert response.headers["Location"] == "/projects?done=tests"
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
    assert response.headers["Location"] == "/projects?done=deps"
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
