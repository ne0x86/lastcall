"""GitHub through the instance's own GitHub App: app token, installation tokens, repositories and their files.

Each instance creates its own private App with the manifest flow (`lastcall github setup`). The App authors the pull
requests, so the owner can approve them. Its key lives in the secret store; tokens never go into argv or .git/config.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lastcall.secretstore import SecretStore

API = "https://api.github.com"
APP_SECRET = "github-app"
PERMISSIONS = {"contents": "write", "pull_requests": "write", "issues": "write", "metadata": "read"}

Response = tuple[int, Mapping[str, str], bytes]
Fetch = Callable[[urllib.request.Request], Response]


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub answered {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class App:
    id: int
    slug: str
    name: str
    owner: str
    html_url: str
    pem: str

    @property
    def install_url(self) -> str:
        return f"https://github.com/apps/{self.slug}/installations/new"

    @classmethod
    def load(cls, store: SecretStore) -> App | None:
        data = store.get_json(APP_SECRET)
        return cls(**data) if data else None

    def save(self, store: SecretStore) -> None:
        store.set_json(APP_SECRET, asdict(self))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app: App, now: float) -> str:
    """The App's own token (RS256 JWT, 10 minutes at most), used to list installations and get their tokens."""
    key = serialization.load_pem_private_key(app.pem.encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GitHubError(0, "the App key is not an RSA key")
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps({"iat": int(now) - 60, "exp": int(now) + 540, "iss": str(app.id)}).encode())
    signature = key.sign(f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{claims}.{_b64url(signature)}"


def urlopen(req: urllib.request.Request) -> Response:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


class GitHub:
    def __init__(self, app: App, fetch: Fetch = urlopen, clock: Callable[[], float] = time.time) -> None:
        self.app, self.fetch, self.clock = app, fetch, clock

    def request(self, method: str, path: str, token: str, body: object = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(API + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        status, _, raw = self.fetch(req)
        value = json.loads(raw) if raw.strip() else {}
        if status >= 400:
            raise GitHubError(status, value.get("message", "") if isinstance(value, dict) else str(value)[:200])
        return value

    def installations(self) -> list[dict[str, Any]]:
        return list(self.request("GET", "/app/installations?per_page=100", app_jwt(self.app, self.clock())))

    def installation_token(self, installation_id: int) -> str:
        jwt = app_jwt(self.app, self.clock())
        return str(self.request("POST", f"/app/installations/{installation_id}/access_tokens", jwt)["token"])

    def repositories(self, token: str) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.request("GET", f"/installation/repositories?per_page=100&page={page}", token)
            repos += data.get("repositories") or []
            if not data.get("repositories") or len(repos) >= int(data.get("total_count", 0)):
                return repos
            page += 1

    def files(self, token: str, repo: str, ref: str) -> tuple[list[tuple[str, int]], bool]:
        """(path, size) of every file at `ref`, and whether GitHub truncated the list. Empty for an empty repo."""
        path = f"/repos/{repo}/git/trees/{urllib.parse.quote(ref, safe='')}?recursive=1"
        try:
            tree = self.request("GET", path, token)
        except GitHubError as e:
            if e.status in (404, 409):  # empty repository, or no such branch yet
                return [], False
            raise
        blobs = [(str(t["path"]), int(t.get("size") or 0)) for t in tree.get("tree") or [] if t.get("type") == "blob"]
        return blobs, bool(tree.get("truncated"))
