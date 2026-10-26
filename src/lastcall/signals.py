"""Errors in production, from Sentry or GlitchTip (DESIGN.md, sections 8 and 9): the unresolved issues of a project
in the owner's error tracker, the most frequent first, each with the frames of its latest event that are the
project's own code.

lastcall reads them with a token that can only read (`project:read` and `event:read`), through the API that the two
trackers share. What reaches a model is the error's type, its message with personal data and secrets masked, its
counts, and file, line, function and code line of the project's frames. Never local variables, request data, user
data, tags or breadcrumbs.

The owner keeps one connection, in the secret store: the tracker's address, the organization and the token. Each
project names its project in the tracker.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from lastcall.quota import epoch
from lastcall.secretscan import RULES
from lastcall.secretstore import SecretStore

SECRET = "signals"  # the connection to the tracker
DAYS = 14  # how far back the audits look
MAX_ERRORS = 8
MAX_FRAMES = 6
DAY = 86_400.0
TITLE = f"Errors in production (Sentry or GlitchTip, last {DAYS} days)"
Fetch = Callable[[str, Mapping[str, str]], Any]  # (url, headers) -> the parsed JSON answer

# an IPv6 address, also shortened with "::"; a time such as 12:30:45 is not one
IPV6 = re.compile(
    r"(?<![\w:])(?=[0-9a-f:]*::|(?:[0-9a-f]{1,4}:){3})[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?![\w:])", re.I
)
MASKS = [
    (re.compile(r"://[^/\s:@]+:[^/\s@]+@"), "://<credentials>@"),
    (re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (IPV6, "<ip>"),
    *((rule, "<secret>") for rule in RULES.values()),
    (re.compile(r"\b[A-Za-z0-9_-]{32,}\b"), "<secret>"),  # long opaque tokens, and ids that may name a person
    (re.compile(r"\b(?:sk|pk|rk)[-_](?:live|test)[-_][A-Za-z0-9_-]{8,}"), "<secret>"),
    (re.compile(r"\b\d{9,}\b"), "<number>"),  # card, phone and account numbers
]


class SignalsError(RuntimeError):
    """The tracker could not be read: the address, the organization, the project or the token is wrong, or it did
    not answer."""


@dataclass(frozen=True)
class Tracker:
    url: str  # https://sentry.io, or the address of a GlitchTip or of a self-hosted Sentry
    org: str  # the organization's slug, as in the tracker's URLs
    token: str

    @classmethod
    def load(cls, secrets: SecretStore) -> Tracker | None:
        data = secrets.get_json(SECRET)
        if not data:
            return None
        return cls(str(data["url"]), str(data["org"]), str(data["token"]))

    def save(self, secrets: SecretStore) -> None:
        secrets.set_json(SECRET, {"url": self.url, "org": self.org, "token": self.token})

    @property
    def api(self) -> str:
        return f"{self.url.rstrip('/')}/api/0"


@dataclass(frozen=True)
class Error:
    title: str  # the type and the masked message: "ZeroDivisionError: division by zero"
    count: int  # events
    users: int
    last_seen: float | None
    frames: tuple[str, ...]  # the project's own frames, the innermost last: "shop/pricing.py:3 in average: ..."
    ref: str  # the tracker's short id, such as SHOP-1


def fetch_json(url: str, headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except urllib.error.HTTPError as e:
        why = {401: "the token is not valid", 403: "the token may not read this", 404: "not found"}
        raise SignalsError(f"{why.get(e.code, f'HTTP {e.code}')}: {url.split('?')[0]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SignalsError(f"no answer from {url.split('/api/')[0]}: {e}") from None
    except (ValueError, http.client.HTTPException):  # what http.client cannot send: its message may hold the token
        raise SignalsError(
            f"the request to {url.split('/api/')[0]} could not be sent: check the address and the token"
        ) from None


def address(text: str) -> bool:
    """Whether the owner's tracker address is one lastcall can read: http or https, with a host and nothing else."""
    parts = urllib.parse.urlsplit(text)
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and not parts.username
        and not parts.query
        and len(text) <= 200
        and not re.search(r"[\x00-\x20\x7f]", text)  # http.client sends no space or line break in a path
    )


def check(tracker: Tracker, fetch: Fetch = fetch_json) -> None:
    """Read one issue of the organization: raises SignalsError when the connection does not work."""
    get(tracker, f"/organizations/{_q(tracker.org)}/issues/?limit=1", fetch)


def errors(
    tracker: Tracker,
    project: str,
    files: Iterable[str],
    now: float,
    fetch: Fetch = fetch_json,
    days: float = DAYS,
    limit: int = MAX_ERRORS,
    frames: bool = True,
) -> list[Error]:
    """The project's unresolved errors seen in the last `days`, the most frequent first. `files` are the paths of
    the repository, to tell its frames by their paths. Without `frames`, it reads no event: one request less per
    error."""
    org = _q(tracker.org)
    found = get(tracker, f"/projects/{org}/{_q(project)}/", fetch)
    pid = _q(str(found.get("id") if isinstance(found, dict) else ""))
    query = f"/organizations/{org}/issues/?project={pid}&query=is%3Aunresolved&statsPeriod={int(days)}d&limit=100"
    issues = [i for i in get(tracker, query, fetch) or [] if isinstance(i, dict)]
    recent = [i for i in issues if (_time(i.get("lastSeen")) or 0) >= now - days * DAY]
    recent.sort(key=lambda i: -_int(i.get("count")))
    known = set(files)
    out = []
    for issue in recent[:limit]:
        own: tuple[str, ...] = ()
        if frames:
            event = get(tracker, f"/organizations/{org}/issues/{_q(str(issue.get('id')))}/events/latest/", fetch)
            own = project_frames(event, known)
        meta = issue.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        kind, value = str(meta.get("type") or ""), str(meta.get("value") or "")
        title = f"{kind}: {value}" if kind else str(issue.get("title") or "")
        out.append(
            Error(
                mask(title)[:200],
                _int(issue.get("count")),
                _int(issue.get("userCount")),
                _time(issue.get("lastSeen")),
                own,
                mask(str(issue.get("shortId") or issue.get("id") or ""))[:40],
            )
        )
    return out


def get(tracker: Tracker, path: str, fetch: Fetch = fetch_json) -> Any:
    return fetch(tracker.api + path, {"Authorization": f"Bearer {tracker.token}", "Accept": "application/json"})


def project_frames(event: Any, files: set[str]) -> tuple[str, ...]:
    """The frames of the event's error that are the project's own code (the tracker's `inApp`), the innermost last;
    all of them when none is marked. Paths become the repository's when they end like one of its files."""
    entries = event.get("entries") if isinstance(event, dict) else None
    exception = next(
        (e.get("data") for e in entries or [] if isinstance(e, dict) and e.get("type") == "exception"), None
    )
    values = exception.get("values") if isinstance(exception, dict) else None
    if not values or not isinstance(values[-1], dict):  # the last value is the error that was raised
        return ()
    stack = values[-1].get("stacktrace") or {}
    frames = [f for f in stack.get("frames") or [] if isinstance(f, dict)]
    own = [f for f in frames if f.get("inApp")] or frames
    out = []
    for frame in own[-MAX_FRAMES:]:
        path = repo_path(str(frame.get("filename") or frame.get("absPath") or "?"), files)
        line = frame.get("lineNo") or frame.get("lineno") or "?"
        code = next((str(c[1]).strip() for c in frame.get("context") or [] if c and c[0] == line), "")
        text = f"{path}:{line} in {frame.get('function') or '?'}" + (f": {code[:120]}" if code else "")
        out.append(mask(text))
    return tuple(out)


def repo_path(name: str, files: set[str]) -> str:
    """The repository's path for a frame's file: the longest ending of its path that is a file of the repository;
    otherwise its last two parts, so that no path of the server leaks."""
    parts = (PureWindowsPath(name) if "\\" in name else PurePosixPath(name)).parts
    for n in range(len(parts), 0, -1):
        candidate = "/".join(parts[-n:])
        if candidate in files:
            return candidate
    return "/".join(p for p in parts[-2:] if p not in ("/", "\\"))


def mask(text: str) -> str:
    """Personal data and secrets out of a text that came from production: emails, addresses, tokens, long numbers."""
    for pattern, replacement in MASKS:
        text = pattern.sub(replacement, text)
    return text


def section(found: Sequence[Error]) -> str:
    """The pack's section: what fails in production, for the reviewers to find the cause."""
    if not found:
        return f"No unresolved errors seen in the last {DAYS} days."
    lines = [
        f"{len(found)} unresolved errors seen in the last {DAYS} days, the most frequent first (messages masked):"
    ]
    for e in found:
        seen = datetime.fromtimestamp(e.last_seen, UTC).strftime("%Y-%m-%d") if e.last_seen else "?"
        users = f", {e.users} users" if e.users else ""
        lines.append(f"- {e.title} ({e.count} events{users}, last seen {seen}; {e.ref})")
        lines += [f"    {frame}" for frame in e.frames]
    return "\n".join(lines)


def _q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def _int(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


def _time(value: object) -> float | None:
    try:
        return epoch(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None
