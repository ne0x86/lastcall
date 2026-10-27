import base64
import html
import http.client
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, ClassVar

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lastcall import cli, github
from lastcall.github import APP_SECRET, App, GitHub, GitHubError, Response, app_jwt, lacking
from lastcall.github_setup import SetupServer, clean_owner, form_action, manifest, owner_kind, run_setup
from lastcall.secretstore import SecretStore
from lastcall.store import Store

NOW = 1_800_000_000.0


@pytest.fixture(scope="module")
def key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def app(key: rsa.RSAPrivateKey) -> App:
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return App(42, "lastcall-ne0x86", "lastcall-ne0x86", "ne0x86", "https://github.com/apps/lastcall-ne0x86", pem)


def unb64(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


# app token


def test_app_jwt_is_signed_by_the_app_key(app: App, key: rsa.RSAPrivateKey) -> None:
    header, claims, signature = app_jwt(app, NOW).split(".")
    assert json.loads(unb64(header)) == {"alg": "RS256", "typ": "JWT"}
    assert json.loads(unb64(claims)) == {"iat": int(NOW) - 60, "exp": int(NOW) + 540, "iss": "42"}
    key.public_key().verify(unb64(signature), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def test_app_round_trip(app: App, tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    app.save(store)
    assert App.load(store) == app
    assert app.install_url == "https://github.com/apps/lastcall-ne0x86/installations/new"


def test_an_app_saved_before_it_could_return_loads_as_one_that_does_not(app: App, tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    before = asdict(app)
    del before["returns"]  # what the App's file held before GitHub could bring the owner back
    store.set_json(APP_SECRET, before)
    loaded = App.load(store)
    assert loaded == app and loaded is not None and loaded.returns is False
    replace(app, returns=True).save(store)
    returning = App.load(store)
    assert returning is not None and returning.returns is True


# API


class FakeGitHub:
    """Answers by method and path; records what was asked."""

    def __init__(self, answers: dict[str, tuple[int, object]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, req: urllib.request.Request) -> Response:
        path = req.full_url.removeprefix("https://api.github.com")
        self.calls.append((req.get_method(), path, req.get_header("Authorization")))
        status, body = self.answers[f"{req.get_method()} {path}"]
        return status, {}, json.dumps(body).encode()


def test_installation_token_uses_the_app_jwt(app: App) -> None:
    fake = FakeGitHub({"POST /app/installations/7/access_tokens": (201, {"token": "ghs_x"})})
    assert GitHub(app, fake, lambda: NOW).installation_token(7) == "ghs_x"
    assert fake.calls[0][2] == f"Bearer {app_jwt(app, NOW)}"


def test_repositories_follow_the_pages(app: App) -> None:
    fake = FakeGitHub(
        {
            "GET /installation/repositories?per_page=100&page=1": (200, {"total_count": 3, "repositories": [{}, {}]}),
            "GET /installation/repositories?per_page=100&page=2": (200, {"total_count": 3, "repositories": [{}]}),
        }
    )
    assert len(GitHub(app, fake).repositories("ghs_x")) == 3


def test_files_of_an_empty_repository(app: App) -> None:
    fake = FakeGitHub({"GET /repos/o/r/git/trees/main?recursive=1": (409, {"message": "Git Repository is empty."})})
    assert GitHub(app, fake).files("ghs_x", "o/r", "main") == ([], False)


def test_files_keep_blobs_only(app: App) -> None:
    tree = {
        "truncated": False,
        "tree": [{"path": "src", "type": "tree"}, {"path": "src/a.py", "type": "blob", "size": 9}],
    }
    fake = FakeGitHub({"GET /repos/o/r/git/trees/feature%2Fx?recursive=1": (200, tree)})
    assert GitHub(app, fake).files("ghs_x", "o/r", "feature/x") == ([("src/a.py", 9)], False)


def test_errors_carry_the_status(app: App) -> None:
    fake = FakeGitHub({"POST /app/installations/7/access_tokens": (401, {"message": "Bad credentials"})})
    with pytest.raises(GitHubError, match="401: Bad credentials"):
        GitHub(app, fake).installation_token(7)


def test_repository_token_asks_for_the_installation_and_then_for_its_token(app: App) -> None:
    fake = FakeGitHub(
        {
            "GET /repos/o/r/installation": (200, {"id": 7, "permissions": {"contents": "write"}}),
            "POST /app/installations/7/access_tokens": (201, {"token": "ghs_x"}),
        }
    )
    assert GitHub(app, fake, lambda: NOW).repository_token("o/r") == "ghs_x"
    assert [call[:2] for call in fake.calls] == [
        ("GET", "/repos/o/r/installation"),
        ("POST", "/app/installations/7/access_tokens"),
    ]
    assert {call[2] for call in fake.calls} == {f"Bearer {app_jwt(app, NOW)}"}  # the App's own token, twice


def test_an_app_that_is_not_installed_says_where_to_install_it(app: App) -> None:
    fake = FakeGitHub({"GET /repos/o/r/installation": (404, {"message": "Not Found"})})
    with pytest.raises(GitHubError) as error:
        GitHub(app, fake).repository_token("o/r")
    assert error.value.status == 404
    assert str(error.value).endswith(
        "lastcall-ne0x86 is not installed on o/r: https://github.com/apps/lastcall-ne0x86/installations/new"
    )


def test_granted_is_what_the_installation_was_granted(app: App) -> None:
    permissions = {"contents": "write", "checks": "read", "metadata": "read"}
    fake = FakeGitHub({"GET /repos/o/r/installation": (200, {"id": 7, "permissions": permissions})})
    client = GitHub(app, fake, lambda: NOW)
    assert client.granted("o/r") == permissions
    assert fake.calls == [("GET", "/repos/o/r/installation", f"Bearer {app_jwt(app, NOW)}")]
    for answer in ({"id": 7}, {"id": 7, "permissions": None}, {"id": 7, "permissions": ["checks"]}):
        fake.answers["GET /repos/o/r/installation"] = (200, answer)
        assert client.granted("o/r") is None  # GitHub does not say


@pytest.mark.parametrize(
    ("granted", "missing"),
    [
        ({"checks": "read", "statuses": "read"}, []),
        ({"checks": "write", "statuses": "write"}, []),  # write includes read
        ({"checks": "read"}, ["statuses"]),
        ({"statuses": "write"}, ["checks"]),
        ({"contents": "write", "metadata": "read"}, ["checks", "statuses"]),
        ({}, ["checks", "statuses"]),
    ],
)
def test_lacking_names_what_an_installation_cannot_read(granted: dict[str, str], missing: list[str]) -> None:
    assert lacking(granted) == missing


def test_the_apps_permissions_page_is_a_persons_or_an_organizations(app: App) -> None:
    assert app.permissions_url(False) == "https://github.com/settings/apps/lastcall-ne0x86/permissions"
    assert (
        app.permissions_url(True)
        == "https://github.com/organizations/ne0x86/settings/apps/lastcall-ne0x86/permissions"
    )


@pytest.mark.parametrize(
    ("installation", "org"),
    [
        ({"account": {"login": "ne0x86", "type": "User"}}, False),
        ({"account": {"login": "NE0X86", "type": "Organization"}}, True),  # a login has no case
        ({"account": {"login": "ne0x86"}, "target_type": "Organization"}, True),
        ({"account": {"login": "other", "type": "Organization"}}, None),  # an App made public, installed elsewhere
        ({"account": {"login": "ne0x86", "type": "Bot"}}, None),
        ({"account": {"slug": "an-enterprise", "name": "An enterprise"}}, None),  # an enterprise has no login
        ({"account": None}, None),
        ({}, None),
    ],
)
def test_the_account_of_an_installation_says_whether_an_organization_owns_the_app(
    app: App, installation: dict[str, Any], org: bool | None
) -> None:
    assert app.org_owns(installation) is org  # a private App is installed only where its owner is


# manifest flow


def test_manifest_asks_only_what_lastcall_needs() -> None:
    m = manifest("ne0x86", "http://127.0.0.1:8765/callback")
    assert m["name"] == "lastcall-ne0x86"
    assert m["public"] is False
    assert m["hook_attributes"]["active"] is False
    assert m["default_events"] == []
    assert m["default_permissions"] == {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "checks": "read",  # merge on approve and the watch read the checks of a commit...
        "statuses": "read",  # ...and its commit statuses
        "metadata": "read",
    }
    assert len(manifest("a-very-long-organization-name-indeed", "")["name"]) == 34


def test_manifest_asks_github_to_come_back_after_the_installation_only_with_a_setup_url() -> None:
    plain = manifest("ne0x86", "http://127.0.0.1:8765/callback")
    assert "setup_url" not in plain and "setup_on_update" not in plain
    returning = manifest("ne0x86", "http://127.0.0.1:8765/callback", "http://127.0.0.1:8765/installed")
    assert returning["setup_url"] == "http://127.0.0.1:8765/installed"
    assert returning["setup_on_update"] is True  # adding or removing a repository later brings the owner back too
    assert returning["redirect_url"] == plain["redirect_url"]
    others = {key: value for key, value in returning.items() if key not in ("setup_url", "setup_on_update")}
    assert others == plain  # nothing else changes


def test_form_action_for_an_organization() -> None:
    assert form_action("acme", True, "s").startswith("https://github.com/organizations/acme/settings/apps/new?")


@pytest.fixture
def setup_server(app: App, tmp_path: Path) -> Iterator[SetupServer]:
    conversion = {
        "id": app.id,
        "slug": app.slug,
        "name": app.name,
        "owner": {"login": app.owner},
        "html_url": app.html_url,
        "pem": app.pem,
    }

    def exchange(code: str) -> dict[str, Any]:
        assert code == "one-time-code"
        return conversion

    server = SetupServer(("127.0.0.1", 0), SecretStore(tmp_path), "ne0x86", False, exchange)
    yield server
    server.server_close()


def get(server: SetupServer, path: str) -> tuple[int, str]:
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    worker = threading.Thread(target=server.handle_request)
    worker.start()
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            status, body = r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        status, body = e.code, e.read().decode()
    worker.join(timeout=10)
    return status, body


@pytest.mark.parametrize("org", [False, True])
def test_setup_page_says_who_has_to_be_signed_in_to_github(tmp_path: Path, org: bool) -> None:
    server = SetupServer(("127.0.0.1", 0), SecretStore(tmp_path), "acme", org)
    try:
        _, page = get(server, "/")
    finally:
        server.server_close()
    if org:
        assert "You need to be an owner of acme on GitHub, or be allowed to manage its GitHub Apps." in page
        assert "Sign in to github.com" not in page
    else:
        assert "Sign in to github.com as acme before you continue" in page and "owner of acme" not in page


def test_setup_page_posts_the_manifest_to_github(setup_server: SetupServer) -> None:
    status, page = get(setup_server, "/")
    assert status == 200
    action = re.search(r'action="([^"]+)"', page)
    assert action is not None
    assert html.unescape(action.group(1)) == form_action("ne0x86", False, setup_server.state)
    value = re.search(r'name="manifest" value="([^"]+)"', page)
    assert value is not None
    sent = json.loads(html.unescape(value.group(1)))
    assert sent["redirect_url"] == f"http://127.0.0.1:{setup_server.server_address[1]}/callback"
    assert "setup_url" not in sent and "setup_on_update" not in sent  # this page stops once the App exists


def test_callback_with_a_wrong_state_is_refused(setup_server: SetupServer) -> None:
    status, _ = get(setup_server, "/callback?code=one-time-code&state=forged")
    assert status == 400
    assert setup_server.app is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_callback_saves_the_app_for_lastcall_only(setup_server: SetupServer, app: App, tmp_path: Path) -> None:
    status, page = get(setup_server, f"/callback?code=one-time-code&state={setup_server.state}")
    assert status == 200
    assert app.install_url in page
    assert App.load(SecretStore(tmp_path)) == app
    assert (tmp_path / "github-app.secret").stat().st_mode & 0o777 == 0o600


def test_the_command_saves_an_app_that_github_does_not_bring_back(setup_server: SetupServer, tmp_path: Path) -> None:
    status, _ = get(setup_server, f"/callback?code=one-time-code&state={setup_server.state}")
    saved = App.load(SecretStore(tmp_path))
    assert status == 200 and saved is not None and saved.returns is False


def test_callback_records_the_owner_that_github_says(tmp_path: Path, app: App) -> None:
    # for a person, GitHub creates the App under whoever is signed in, whatever name the command was given
    conversion = {
        "id": app.id,
        "slug": app.slug,
        "name": app.name,
        "owner": {"login": "someone-else"},
        "html_url": app.html_url,
        "pem": app.pem,
    }
    server = SetupServer(("127.0.0.1", 0), SecretStore(tmp_path), "ne0x86", False, lambda code: conversion)
    try:
        status, _ = get(server, f"/callback?code=one-time-code&state={server.state}")
    finally:
        server.server_close()
    saved = App.load(SecretStore(tmp_path))
    assert status == 200 and saved is not None and saved.owner == "someone-else"


def test_callback_without_an_owner_in_the_answer_is_a_bad_gateway(tmp_path: Path, app: App) -> None:
    conversion = {"id": app.id, "slug": app.slug, "name": app.name, "html_url": app.html_url, "pem": app.pem}
    server = SetupServer(("127.0.0.1", 0), SecretStore(tmp_path), "ne0x86", False, lambda code: conversion)
    try:
        status, _ = get(server, f"/callback?code=one-time-code&state={server.state}")
    finally:
        server.server_close()
    assert status == 502 and App.load(SecretStore(tmp_path)) is None


def test_run_setup_ends_when_the_app_arrives(setup_server: SetupServer, app: App) -> None:
    result: list[App] = []
    worker = threading.Thread(target=lambda: result.append(run_setup(setup_server)))
    worker.start()
    url = f"http://127.0.0.1:{setup_server.server_address[1]}/callback?code=one-time-code&state={setup_server.state}"
    with urllib.request.urlopen(url, timeout=10):
        pass
    worker.join(timeout=10)
    assert result == [app]


# who owns the repositories: a person, an organization, or nobody


def users_api(
    status: int, body: object
) -> tuple[Callable[[urllib.request.Request], Response], list[urllib.request.Request]]:
    asked: list[urllib.request.Request] = []

    def fetch(req: urllib.request.Request) -> Response:
        asked.append(req)
        return status, {}, body if isinstance(body, bytes) else json.dumps(body).encode()

    return fetch, asked


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"type": "User"}, "user"),
        (200, {"type": "Organization"}, "org"),
        (404, {"message": "Not Found"}, "missing"),
        (403, {"message": "API rate limit exceeded"}, None),
        (500, {}, None),
        (200, {"type": "Bot"}, None),
        (200, {"login": "acme"}, None),
        (200, [1, 2], None),
        (200, b"<html>not json</html>", None),
        (200, b"", None),
    ],
)
def test_owner_kind_reads_what_github_says(status: int, body: object, expected: str | None) -> None:
    fetch, asked = users_api(status, body)
    assert owner_kind("acme", fetch) == expected
    request = asked[0]
    assert request.full_url == "https://api.github.com/users/acme" and request.get_method() == "GET"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert request.get_header("X-github-api-version") == "2022-11-28"
    assert not request.has_header("Authorization")  # the public API, with no token


def test_owner_kind_is_none_for_a_body_nested_too_deep_to_read() -> None:
    fetch, _ = users_api(200, b"[" * 100_000)  # json.loads raises RecursionError, which is a RuntimeError
    assert owner_kind("acme", fetch) is None


def test_owner_kind_waits_10_seconds_and_every_other_request_30(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    class Answer:
        status = 200
        headers: ClassVar[dict[str, str]] = {}

        def __enter__(self) -> "Answer":
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def read(self) -> bytes:
            return b'{"type": "User"}'

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> Answer:
        waits.append(timeout)
        return Answer()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert owner_kind("acme", github.urlopen) == "user"
    github.urlopen(urllib.request.Request("https://api.github.com/app"))
    assert waits == [10, 30]


@pytest.mark.parametrize(
    "error",
    [
        OSError("no route to host"),
        http.client.IncompleteRead(b'{"ty'),
        http.client.BadStatusLine("garbage"),
        http.client.LineTooLong("header line"),
    ],
)
def test_owner_kind_is_none_when_github_cannot_be_reached(error: Exception) -> None:
    def down(req: urllib.request.Request) -> Response:
        raise error

    assert owner_kind("acme", down) is None


def test_clean_owner_strips_what_people_paste_with_a_name() -> None:
    for typed in (
        "acme",
        " acme ",
        "@acme",
        "https://github.com/acme",
        "https://github.com/acme/",
        "github.com/acme/",
    ):
        assert clean_owner(typed) == "acme"
    assert clean_owner("http://www.github.com/@acme") == "acme"
    assert clean_owner("acme/api") == "acme/api"  # not a name: the pattern refuses it afterwards


@pytest.mark.parametrize(
    "typed",
    [
        "HTTPS://GitHub.com/acme",
        "Https://WWW.GITHUB.COM/acme/",
        "GitHub.com/acme",
        "https://github.com/acme?tab=repositories",
        "https://github.com/acme/?tab=repositories#x",
        "https://github.com/acme#x",
        "github.com/@acme?",
        "https://github.com/orgs/acme",
        "https://github.com/orgs/acme/",
        "https://github.com/orgs/acme?tab=people",
        "github.com/ORGS/acme",
    ],
)
def test_clean_owner_reads_a_profile_address_however_it_is_written(typed: str) -> None:
    assert clean_owner(typed) == "acme"


def test_clean_owner_keeps_the_case_of_the_name_and_what_is_not_an_address() -> None:
    assert clean_owner("HTTPS://github.com/Acme") == "Acme"
    assert clean_owner("orgs/acme") == "orgs/acme"  # only an address of github.com has an organization's page
    assert clean_owner("https://github.com/orgs") == "orgs"  # an account that is named orgs
    assert clean_owner("https://example.com/github.com/acme") == "https://example.com/github.com/acme"


asked: list[str] = []  # the owners that `lastcall github setup` asked GitHub about


def run_github_setup(
    monkeypatch: pytest.MonkeyPatch, kind: str | None, *argv: str, owner: str = "acme"
) -> tuple[int, list[bool]]:
    """`lastcall github setup` with GitHub answering `kind`, up to the moment that it would open the setup page."""
    asked.clear()

    def owner_kind(owner: str) -> str | None:
        asked.append(owner)
        return kind

    monkeypatch.setattr(cli, "owner_kind", owner_kind)
    orgs: list[bool] = []

    def refuse(address: tuple[str, int], store: SecretStore, owner: str, org: bool) -> None:
        orgs.append(org)
        raise ValueError("stop here")

    monkeypatch.setattr(cli, "SetupServer", refuse)
    code = cli.main(["github", "setup", f"--owner={owner}", *argv])
    return code, orgs


@pytest.mark.parametrize(
    ("kind", "argv", "org", "said"),
    [
        ("org", (), True, ""),
        ("user", (), False, ""),
        (None, (), False, "Pass --org"),
        (None, ("--org",), True, ""),
        ("user", ("--org",), True, ""),  # --org overrides what GitHub says
    ],
)
def test_github_setup_asks_github_whether_the_owner_is_an_organization(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str | None,
    argv: tuple[str, ...],
    org: bool,
    said: str,
) -> None:
    code, orgs = run_github_setup(monkeypatch, kind, *argv)
    assert code == 2 and orgs == [org]  # the stub refuses to open the page, after the choice is made
    out = capsys.readouterr()
    assert (said in out.out) and ("GitHub did not say" in out.out) == bool(said)


def test_github_setup_stops_on_a_name_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, orgs = run_github_setup(monkeypatch, "missing")
    assert code == 2 and orgs == []
    assert "no account or organization called acme" in capsys.readouterr().err


@pytest.mark.parametrize("typed", ["a b", "-acme", "acme/api", "ac_me", "x" * 40, "@", "https://github.com/"])
def test_github_setup_checks_the_name_before_it_asks_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], typed: str
) -> None:
    code, orgs = run_github_setup(monkeypatch, "missing", owner=typed)
    assert code == 2 and orgs == [] and asked == []  # GitHub is not asked about a name it cannot have
    err = capsys.readouterr().err
    assert (
        "is not the name of a GitHub account or organization" in err
        and "no account or organization called" not in err
    )


@pytest.mark.parametrize(
    "typed", ["@acme", " acme ", "https://github.com/acme", "github.com/acme/", "www.github.com/@acme"]
)
def test_github_setup_accepts_the_owner_as_the_web_page_does(monkeypatch: pytest.MonkeyPatch, typed: str) -> None:
    code, orgs = run_github_setup(monkeypatch, "org", owner=typed)
    assert code == 2 and orgs == [True] and asked == ["acme"]  # the stub refuses the page, after the cleaned name


@pytest.mark.parametrize(
    ("published", "shown"),
    [
        ("127.0.0.1:8080", "http://127.0.0.1:8080/"),
        ("192.168.1.20:9000", "http://192.168.1.20:9000/"),
        ("lastcall.lan:8080", "http://lastcall.lan:8080/"),
        ("0.0.0.0:8080", "port 8080 of this server's addresses, such as http://<this server's address>:8080/"),
        (":::8081", "port 8081 of this server's addresses, such as http://<this server's address>:8081/"),
        ("[::1]:8080", "http://[::1]:8080/"),
        ("host:65535", "http://host:65535/"),
        ("host:0", None),
        ("host:65536", None),
        ("host:" + "".join(map(chr, (0xFF18, 0xFF10))), None),  # full-width digits are digits to str.isdigit()
        ("host:" + "".join(map(chr, (0x0668, 0x0660))), None),
        ("", None),
        ("8080", None),
        ("host:", None),
    ],
)
def test_the_log_says_where_docker_publishes_the_web_ui(published: str, shown: str | None) -> None:
    assert cli.published_address(published) == shown


def test_the_log_sends_a_docker_user_to_the_web_ui_before_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class Stop(Exception):
        pass

    def stop(seconds: float) -> None:
        raise Stop

    monkeypatch.setattr(time, "sleep", stop)
    st = Store(tmp_path / "state.db")
    with pytest.raises(Stop):
        cli.schedule(st, SecretStore(tmp_path / "secrets"))
    line = capsys.readouterr().out
    assert line.startswith(
        "Waiting for the GitHub App: sign in to the web UI and open GitHub App, from Getting started"
    )
    assert "or from Settings" in line and line.index("web UI") < line.index("`lastcall github setup --owner <login>`")
