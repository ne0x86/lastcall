import re
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from lastcall.quota import HOUR, Reading, Window
from lastcall.recipes.audit import AuditReport
from lastcall.secretstore import SecretStore
from lastcall.store import Store
from lastcall.web import CSP, create_app
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

NOW = 1_800_000_000.0
DAY = 86_400.0
SITE = {"Origin": "http://localhost"}
PASSWORD = "correct horse battery staple"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Web:
    def __init__(self, tmp_path: Path, origins: tuple[str, ...] = ()) -> None:
        self.clock = Clock()
        self.store = Store(tmp_path / "state" / "lastcall.db")
        self.secrets = SecretStore(tmp_path / "secrets")
        self.logs: list[str] = []
        self.app = create_app(self.store, self.secrets, clock=self.clock, log=self.logs.append, origins=origins)
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
