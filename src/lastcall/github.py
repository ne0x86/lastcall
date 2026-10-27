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
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from lastcall import git
from lastcall.secretstore import SecretStore

API = "https://api.github.com"
APP_SECRET = "github-app"
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",  # merge on approve and the watch read the check runs of a commit...
    "statuses": "read",  # ...and its commit statuses (lastcall.approval)
    "metadata": "read",
}
CHECKS_PERMISSIONS = ("checks", "statuses")  # what GitHub's page calls "Checks" and "Commit statuses"

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

    def permissions_url(self, org: bool) -> str:
        """The page where the App's owner edits its permissions on GitHub: an organization has its own address."""
        if org:
            return f"https://github.com/organizations/{self.owner}/settings/apps/{self.slug}/permissions"
        return f"https://github.com/settings/apps/{self.slug}/permissions"

    def org_owns(self, installation: Mapping[str, Any]) -> bool | None:
        """Whether an organization owns the App (False: a person), from one of its installations as GitHub returns
        it: lastcall keeps only the owner's name, and a private App can only be installed on the account that owns
        it. None when the installation is of another account, or does not say."""
        account = installation.get("account")
        if not isinstance(account, dict):
            return None
        login = account.get("login")
        if not isinstance(login, str) or login.lower() != self.owner.lower():
            return None
        kind = account.get("type") or installation.get("target_type")
        return kind == "Organization" if kind in ("Organization", "User") else None

    @classmethod
    def load(cls, store: SecretStore) -> App | None:
        data = store.get_json(APP_SECRET)
        return cls(**data) if data else None

    def save(self, store: SecretStore) -> None:
        store.set_json(APP_SECRET, asdict(self))


def permissions_of(installation: Mapping[str, Any]) -> dict[str, str] | None:
    """What an installation, as GitHub returns it, was granted, such as {"contents": "write", "checks": "read"}. A
    permission that its owner has not accepted yet is not in it. None when GitHub does not say."""
    found = installation.get("permissions")
    return {str(name): str(level) for name, level in found.items()} if isinstance(found, dict) else None


def lacking(granted: Mapping[str, str]) -> list[str]:
    """Of `CHECKS_PERMISSIONS`, what an installation was not granted. Reading takes "read"; "write" includes it."""
    return [name for name in CHECKS_PERMISSIONS if granted.get(name) not in ("read", "write")]


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
    """GitHub answers within 30 seconds, or within `req.timeout` when the caller set a shorter wait on the request."""
    try:
        with urllib.request.urlopen(req, timeout=getattr(req, "timeout", None) or 30) as r:
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

    def installation(self, repo: str) -> dict[str, Any]:
        """The App's installation on the owner of owner/name as GitHub returns it, with what it was granted: the App
        must be installed there."""
        try:
            return dict(self.request("GET", f"/repos/{repo}/installation", app_jwt(self.app, self.clock())))
        except GitHubError as e:
            if e.status == 404:
                raise GitHubError(404, f"{self.app.name} is not installed on {repo}: {self.app.install_url}") from e
            raise

    def repository_token(self, repo: str) -> str:
        """An installation token for owner/name: the App must be installed on it."""
        return self.installation_token(int(self.installation(repo)["id"]))

    def granted(self, repo: str) -> dict[str, str] | None:
        """What the installation on the owner of owner/name was granted (see `permissions_of`)."""
        return permissions_of(self.installation(repo))

    def repository(self, token: str, repo: str) -> dict[str, Any]:
        return dict(self.request("GET", f"/repos/{repo}", token))

    def bot_author(self, token: str) -> tuple[str, str]:
        """Name and email that make commits show as the App's bot on GitHub."""
        login = f"{self.app.slug}[bot]"
        user = self.request("GET", f"/users/{urllib.parse.quote(login)}", token)
        return login, f"{user['id']}+{login}@users.noreply.github.com"

    def open_pull_request(self, token: str, repo: str, head: str, base: str, title: str, body: str) -> str:
        pr = self.request(
            "POST", f"/repos/{repo}/pulls", token, {"title": title, "head": head, "base": base, "body": body}
        )
        return str(pr["html_url"])

    def open_issue(self, token: str, repo: str, title: str, body: str) -> str:
        return str(self.request("POST", f"/repos/{repo}/issues", token, {"title": title, "body": body})["html_url"])

    def repositories(self, token: str) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        page = 1
        while True:
            data = self.request("GET", f"/installation/repositories?per_page=100&page={page}", token)
            repos += data.get("repositories") or []
            if not data.get("repositories") or len(repos) >= int(data.get("total_count", 0)):
                return repos
            page += 1

    def labelled_issues(self, token: str, repo: str, label: str) -> list[dict[str, Any]]:
        """The open issues with the label, without pull requests: their number, url and title."""
        path = f"/repos/{repo}/issues?state=open&labels={urllib.parse.quote(label)}&per_page=100"
        issues = [i for i in self.request("GET", path, token) if "pull_request" not in i]
        return [{"number": i["number"], "url": i["html_url"], "title": i["title"]} for i in issues]

    def ensure_label(self, token: str, repo: str, label: str, description: str) -> None:
        """Create the label in the repository, unless it has it already."""
        try:
            self.request("GET", f"/repos/{repo}/labels/{urllib.parse.quote(label)}", token)
        except GitHubError as e:
            if e.status != 404:
                raise
            body = {"name": label, "color": "0e8a16", "description": description}
            self.request("POST", f"/repos/{repo}/labels", token, body)

    def unlabel(self, token: str, repo: str, number: int, label: str) -> None:
        """Take the label off an issue; an issue without it is fine."""
        try:
            self.request("DELETE", f"/repos/{repo}/issues/{number}/labels/{urllib.parse.quote(label)}", token)
        except GitHubError as e:
            if e.status != 404:
                raise

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


@dataclass(frozen=True)
class GitHubForge:
    """Where the audit delivers. Every call gets a fresh installation token: a run can outlast one (one hour)."""

    client: GitHub
    repo: str
    base: str  # the branch pull requests target
    author: tuple[str, str]  # the App's bot, so commits show as lastcall's

    @classmethod
    def connect(cls, client: GitHub, repo: str) -> GitHubForge:
        token = client.repository_token(repo)
        return cls(client, repo, str(client.repository(token, repo)["default_branch"]), client.bot_author(token))

    def push(self, workdir: Path, branch: str) -> None:
        git.push(workdir, self.repo, branch, self.client.repository_token(self.repo))

    def open_pull_request(self, branch: str, title: str, body: str) -> str:
        token = self.client.repository_token(self.repo)
        return self.client.open_pull_request(token, self.repo, branch, self.base, title, body)

    def open_issue(self, title: str, body: str) -> str:
        return self.client.open_issue(self.client.repository_token(self.repo), self.repo, title, body)

    def issue(self, number: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """An issue as GitHub returns it, and its comments, up to the latest 100."""
        token = self.client.repository_token(self.repo)
        found = self.client.request("GET", f"/repos/{self.repo}/issues/{number}", token)
        comments = self.client.request("GET", f"/repos/{self.repo}/issues/{number}/comments?per_page=100", token)
        return dict(found), list(comments)

    def comment(self, number: int, body: str) -> None:
        token = self.client.repository_token(self.repo)
        self.client.request("POST", f"/repos/{self.repo}/issues/{number}/comments", token, {"body": body})

    def unlabel(self, number: int, label: str) -> None:
        self.client.unlabel(self.client.repository_token(self.repo), self.repo, number, label)
