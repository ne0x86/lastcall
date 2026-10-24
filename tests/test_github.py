import base64
import html
import json
import re
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lastcall.github import App, GitHub, GitHubError, Response, app_jwt
from lastcall.github_setup import SetupServer, form_action, manifest, run_setup
from lastcall.secretstore import SecretStore

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
        "metadata": "read",
    }
    assert len(manifest("a-very-long-organization-name-indeed", "")["name"]) == 34


def test_form_action_for_an_organization() -> None:
    assert form_action("acme", True, "s").startswith("https://github.com/organizations/acme/settings/apps/new?")


@pytest.fixture
def setup_server(app: App, tmp_path: Path) -> Iterator[SetupServer]:
    conversion = {"id": app.id, "slug": app.slug, "name": app.name, "html_url": app.html_url, "pem": app.pem}

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


def test_run_setup_ends_when_the_app_arrives(setup_server: SetupServer, app: App) -> None:
    result: list[App] = []
    worker = threading.Thread(target=lambda: result.append(run_setup(setup_server)))
    worker.start()
    url = f"http://127.0.0.1:{setup_server.server_address[1]}/callback?code=one-time-code&state={setup_server.state}"
    with urllib.request.urlopen(url, timeout=10):
        pass
    worker.join(timeout=10)
    assert result == [app]
