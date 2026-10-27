"""Errors in production, from Sentry or GlitchTip: what lastcall asks the tracker, what it keeps, and what it masks.
The answers have the shape that a GlitchTip 6.2 instance gave for errors sent with the Sentry SDK."""

import http.client
import io
import json
import time
import urllib.error
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from lastcall import signals
from lastcall.github import GitHub
from lastcall.ranking import rank
from lastcall.runs import Instance, production_errors
from lastcall.sandbox import Sandbox
from lastcall.secretstore import SecretStore
from lastcall.store import ProjectRow, Store

NOW = 1_800_000_000.0  # 2027-01-15 08:00 UTC
TRACKER = signals.Tracker("https://glitchtip.example.com", "acme", "t0ken")


def frame(path: str, line: int, function: str, code: str, in_app: bool = True) -> dict[str, Any]:
    return {
        "filename": path,
        "absPath": path,
        "lineNo": line,
        "function": function,
        "inApp": in_app,
        "context": [[line - 1, "    total = sum(prices)"], [line, code]],
        "vars": {"prices": "[]", "password": "hunter2"},  # never read
    }


EVENT = {
    "entries": [
        {
            "type": "exception",
            "data": {
                "values": [
                    {
                        "type": "ZeroDivisionError",
                        "value": "division by zero",
                        "stacktrace": {
                            "frames": [
                                frame("/usr/lib/python3.12/runpy.py", 88, "_run_code", "exec(code)", in_app=False),
                                frame("/srv/shop/shop/views.py", 21, "checkout", "average(prices)"),
                                frame("/srv/shop/shop/pricing.py", 3, "average", "    return total / len(prices)"),
                            ]
                        },
                    }
                ]
            },
        },
        {"type": "request", "data": {"headers": [["Cookie", "session=secret"]]}},
        {"type": "breadcrumbs", "data": {"values": [{"message": "user maria@example.com logged in"}]}},
    ],
    "user": {"email": "maria@example.com"},
}


def issue(n: int, count: int, last_seen: str, kind: str, value: str) -> dict[str, Any]:
    return {
        "id": str(n),
        "shortId": f"SHOP-{n}",
        "title": f"{kind}: {value}",
        "count": str(count),
        "userCount": 2,
        "lastSeen": last_seen,
        "metadata": {"type": kind, "value": value, "filename": "/srv/shop/shop/pricing.py"},
    }


class Tracker:
    """A stand-in for the tracker's API, which records what lastcall asks."""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues, self.asked = issues, list[tuple[str, dict[str, str]]]()

    def __call__(self, url: str, headers: Mapping[str, str]) -> Any:
        self.asked.append((url, dict(headers)))
        path = url.removeprefix("https://glitchtip.example.com/api/0")
        if path == "/projects/acme/shop/":
            return {"id": "7", "slug": "shop"}
        if path.startswith("/organizations/acme/issues/?"):
            return self.issues
        if path.endswith("/events/latest/"):
            return EVENT
        raise signals.SignalsError(f"not found: {url}")


ISSUES = [
    issue(1, 5, "2027-01-13T10:00:00Z", "ZeroDivisionError", "division by zero"),
    issue(2, 40, "2027-01-15T07:00:00.123Z", "LookupError", "no user maria.garcia@example.com from 10.0.0.12"),
    issue(3, 99, "2026-11-01T00:00:00Z", "OldError", "seen long ago"),
]


def test_the_most_frequent_errors_with_the_projects_own_frames() -> None:
    tracker = Tracker(ISSUES)
    found = signals.errors(TRACKER, "shop", {"shop/pricing.py", "shop/views.py"}, NOW, tracker)
    assert [e.ref for e in found] == ["SHOP-2", "SHOP-1"]  # the most frequent first; the old one is left out
    assert found[0].title == "LookupError: no user <email> from <ip>"
    assert found[1].frames == (
        "shop/views.py:21 in checkout: average(prices)",
        "shop/pricing.py:3 in average: return total / len(prices)",
    )
    asked = [url for url, _ in tracker.asked]
    assert asked[0] == "https://glitchtip.example.com/api/0/projects/acme/shop/"
    assert asked[1] == (
        "https://glitchtip.example.com/api/0/organizations/acme/issues/"
        "?project=7&query=is%3Aunresolved&statsPeriod=14d&limit=100"
    )
    assert asked[2:] == [
        "https://glitchtip.example.com/api/0/organizations/acme/issues/2/events/latest/",
        "https://glitchtip.example.com/api/0/organizations/acme/issues/1/events/latest/",
    ]
    assert {headers["Authorization"] for _, headers in tracker.asked} == {"Bearer t0ken"}
    text = signals.section(found)
    assert "2 unresolved errors seen in the last 14 days" in text
    assert "- LookupError: no user <email> from <ip> (40 events, 2 users, last seen 2027-01-15; SHOP-2)" in text
    for private in ("maria", "10.0.0.12", "hunter2", "session=secret", "/srv/"):
        assert private not in text


def test_counting_reads_no_event() -> None:
    tracker = Tracker(ISSUES)
    found = signals.errors(TRACKER, "shop", (), NOW, tracker, days=1, limit=100, frames=False)
    assert [e.ref for e in found] == ["SHOP-2"]  # only what was seen in the last day
    assert not any(url.endswith("/events/latest/") for url, _ in tracker.asked)


def test_masks_and_paths() -> None:
    text = (
        "token sk-live-abcdefghijklmnop1234 for https://bob:pw@db.example.com/x, card 4111111111111111, ip fe80::1:2"
    )
    assert signals.mask(text) == "token <secret> for https://<credentials>@db.example.com/x, card <number>, ip <ip>"
    assert signals.mask("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N") == "<secret>"
    assert signals.mask("at 12:30:45 on 2027-01-15") == "at 12:30:45 on 2027-01-15"  # times and dates stay
    files = {"shop/pricing.py", "app.py"}
    assert signals.repo_path("/srv/shop/shop/pricing.py", files) == "shop/pricing.py"
    assert signals.repo_path("C:\\Users\\dev\\shop\\app.py", files) == "app.py"
    assert signals.repo_path("/home/deploy/other/thing.py", files) == "other/thing.py"  # nothing of the server
    assert signals.project_frames({"entries": [{"type": "message"}]}, files) == ()


def test_what_http_client_cannot_send_is_a_tracker_error_that_does_not_show_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for refusal in (
        ValueError("Invalid header value b'Bearer sntrys_SECRET'"),
        http.client.InvalidURL("URL can't contain control characters"),
    ):

        def urlopen(request: Any, timeout: float, refusal: Exception = refusal) -> io.BytesIO:
            raise refusal

        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        with pytest.raises(signals.SignalsError, match="could not be sent") as raised:
            signals.check(signals.Tracker("https://glitchtip.example.com", "acme", "sntrys_SECRET"))
        assert "SECRET" not in str(raised.value) and "Bearer" not in str(raised.value)
    for text in ("http://h/a b", "http://h/a" + chr(10) + "b", "http://h/a" + chr(9), "http://h/a" + chr(127)):
        assert not signals.address(text), repr(text)


def test_an_answer_that_is_not_the_trackers_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: io.BytesIO(b"<html>a proxy's page</html>"))
    with pytest.raises(signals.SignalsError, match="did not answer as Sentry or GlitchTip does"):
        signals.check(TRACKER)


def test_the_command_line_refuses_a_tracker_token_with_a_line_break_inside(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from lastcall import cli

    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LASTCALL_SECRETS", str(tmp_path / "secrets"))
    token = tmp_path / "token"
    token.write_text("sntrys_SECRET" + chr(10) + "TAIL", encoding="utf-8")
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("nothing is sent"))
    with pytest.raises(SystemExit):
        cli.main(
            [
                "signals",
                "connect",
                "--url",
                "https://glitchtip.example.com",
                "--org",
                "acme",
                "--token-file",
                str(token),
            ]
        )
    captured = capsys.readouterr()
    assert "control characters" in captured.err and "SECRET" not in captured.err + captured.out
    assert signals.Tracker.load(SecretStore(tmp_path / "secrets")) is None


def test_the_tracker_says_what_is_wrong(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: Any, timeout: float) -> io.BytesIO:
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(signals.SignalsError, match="the token is not valid"):
        signals.check(TRACKER)
    assert signals.address("https://sentry.io") and signals.address("http://glitchtip:8000")
    assert not signals.address("ftp://sentry.io") and not signals.address("https://user:pw@sentry.io")
    assert not signals.address("https://sentry.io/?x=1")


def test_projects_with_errors_rank_higher() -> None:
    projects = [ProjectRow("acme/shop", True, 1, None, 3), ProjectRow("acme/blog", True, 1, None, 3)]
    ranked = rank(
        projects,
        last_run=lambda repo: {"started": NOW - 10 * 86_400, "sha": ""},
        open_prs=lambda repo: 0,
        pending=lambda repo: 0,
        new_commits=lambda repo, sha: None,
        busy=set(),
        now=NOW,
        errors=lambda repo: 3 if repo == "acme/shop" else 0,
    )
    assert ranked[0].repo == "acme/shop" and "3 errors in production in the last day" in ranked[0].reasons
    assert ranked[0].score == ranked[1].score + 15


def test_the_pack_and_the_service_read_the_tracker(
    tmp_path: Path, make_repo: Callable[[Path, dict[str, str]], Path]
) -> None:
    from lastcall.service import Service

    clone = make_repo(tmp_path / "clone", {"shop/pricing.py": "", "shop/views.py": ""})
    store = Store(tmp_path / "lastcall.db")
    store.add_project("acme/shop")
    store.add_project("acme/blog")
    secrets = SecretStore(tmp_path / "secrets")
    now = time.time()  # a run reads the tracker on the real clock

    def iso(when: float) -> str:
        return datetime.fromtimestamp(when, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    tracker = Tracker(
        [
            issue(1, 5, iso(now - 2 * 86_400), "ZeroDivisionError", "division by zero"),
            issue(2, 40, iso(now - 3600), "LookupError", "no user"),
            issue(3, 99, iso(now - 90 * 86_400), "OldError", "seen long ago"),
        ]
    )
    inst = Instance(cast(Sandbox, None), cast(GitHub, None), store, secrets=secrets, fetch=tracker)
    assert production_errors(inst, "acme/shop", clone) == {}  # no tracker connected yet
    TRACKER.save(secrets)
    assert production_errors(inst, "acme/shop", clone) == {}  # the project has no project in the tracker
    store.set_signals("acme/shop", "shop")
    section = production_errors(inst, "acme/shop", clone)[signals.TITLE]
    assert section.startswith("2 unresolved errors") and "shop/pricing.py:3 in average" in section
    svc = Service(inst, start=lambda work: None, clock=lambda: now, log=lambda m: None)
    svc.read_signals(now)
    assert svc.errors == {"acme/shop": 1}  # one error in the last day; acme/blog has no project in the tracker
    store.set_signals("acme/shop", "gone")
    assert production_errors(inst, "acme/shop", clone) == {signals.TITLE: f"Not read: not found: {TRACKER.api}"
                                                              "/projects/acme/gone/"}  # fmt: skip


def test_saved_the_connection_is_a_secret(tmp_path: Path) -> None:
    secrets = SecretStore(tmp_path / "secrets")
    TRACKER.save(secrets)
    assert signals.Tracker.load(secrets) == TRACKER
    assert json.loads(secrets.get(signals.SECRET) or "{}")["token"] == "t0ken"
