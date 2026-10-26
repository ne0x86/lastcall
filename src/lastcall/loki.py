"""Errors in the logs that Grafana Loki keeps (DESIGN.md, section 8): for apps on other machines, with nothing to
install there, when Loki already collects their logs (with Grafana Alloy, Promtail or Docker's Loki driver).

The owner connects Loki once: its address and, when it asks for one, a login that can read. lastcall lists the
sources that Loki saw in the last 7 days, named by one of their labels (`container` when there is one), and the owner
chooses the project of each, as with the containers on lastcall's own host (`lastcall.logs`), with the same
suggestions. lastcall reads no source until then.

For each chosen source, lastcall asks Loki for the lines of the last day that may be part of an error (a filter that
Loki applies: error levels, tracebacks, stacks), the newest 5,000, and groups them into kinds of errors as
`lastcall.logs` does. What reaches a model is masked in the same way: one sample line per kind, how often and when,
and the file and line of its frames.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lastcall import logs
from lastcall.secretstore import SecretStore
from lastcall.supervisor import STEP

if TYPE_CHECKING:
    from lastcall.store import Store

SECRET = "loki"  # the connection, in the secret store
LABEL = "loki_label"  # the label that names a source, as the owner's choices use it
CHOSEN = "loki_sources"  # the owner's choices: source -> owner/name, "" for a no
NAMES = ("container", "container_name", "service_name", "app", "job")  # labels that name a source, the best first
UNKNOWN = "unknown_service"  # Loki's service_name for a stream that names none
METADATA = {"__stream_shard__", "detected_level"}  # Loki's own labels, not the stream's
LABEL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SOURCE = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
TENANT = re.compile(r"[A-Za-z0-9!._*'()|-]{0,150}")
LIST_DAYS = 7  # how far back the list of sources looks
DAY = 86_400
LIMIT = 5000  # lines per source, the newest: Loki's own limit by default
MAX_SOURCES = 300
MAX_LINE = 2000  # characters of a line that lastcall reads
MAX_ANSWER = 20 * 1024 * 1024
TITLE = f"Errors in the logs in Loki (last {logs.HOURS} hours)"
Fetch = Callable[[str, Mapping[str, str]], Any]  # (url, headers) -> the parsed JSON answer

# The lines that may be part of an error, as lastcall.logs reads them: Loki keeps only these (RE2), so that the
# newest 5,000 cover more of the day
FILTER = "|".join(
    (
        r"Traceback \(most recent call last\)",
        r"During handling of the above exception|The above exception was the direct cause",
        r'^\s+(File "|at )',  # the frames of a stack
        r"^(Uncaught )?[A-Za-z_][\w.]*(Error|Exception)\b",
        r"^panic: |^Exception in thread ",
        r"\b(ERROR|CRITICAL|FATAL|PANIC|SEVERE|EMERG)\b",
        r"(?i:\blevel[\"']?\s*[=:]\s*[\"']?(error|err|critical|crit|fatal|panic)\b|\[(error|crit|alert|emerg)\])",
        # the last line of a Python traceback names its exception: CamelCase of two words or more, or after its
        # module (`httpx.ReadTimeout`, `KeyboardInterrupt`), so that `INFO: ...` and `Date: ...` stay out
        r"^([a-z_]\w*\.)*[A-Z][a-z0-9]+[A-Z]\w*(: |$)|^([a-z_]\w*\.)+[A-Z]\w*(: |$)",
    )
)


class LokiError(RuntimeError):
    """Loki could not be read: the address or the login is wrong, a query failed, or it did not answer."""


@dataclass(frozen=True)
class Loki:
    url: str  # such as http://192.168.1.20:3100, or https://logs-prod-012.grafana.net in Grafana Cloud
    user: str = ""  # for a login: Grafana Cloud's user number, or the user of a proxy in front of Loki
    token: str = ""  # the login's password or token; a bearer token when there is no user
    tenant: str = ""  # X-Scope-OrgID, for a Loki with more than one tenant

    @classmethod
    def load(cls, secrets: SecretStore) -> Loki | None:
        data = secrets.get_json(SECRET)
        if not data:
            return None
        return cls(
            str(data["url"]), str(data.get("user") or ""), str(data.get("token") or ""), str(data.get("tenant") or "")
        )

    def save(self, secrets: SecretStore) -> None:
        secrets.set_json(SECRET, {"url": self.url, "user": self.user, "token": self.token, "tenant": self.tenant})

    def headers(self) -> dict[str, str]:
        # categorize-labels: each line's metadata, such as the level that Loki detects, apart from the stream's
        # labels, so that the lines of a traceback stay in one stream
        out = {"Accept": "application/json", "X-Loki-Response-Encoding-Flags": "categorize-labels"}
        if self.user:
            out["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
        elif self.token:
            out["Authorization"] = f"Bearer {self.token}"
        if self.tenant:
            out["X-Scope-OrgID"] = self.tenant
        return out


def fetch_json(url: str, headers: Mapping[str, str]) -> Any:
    where = url.split("/loki/api/")[0]
    request = urllib.request.Request(url, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as e:
        why = {
            401: "the user or the token is not valid",
            403: "this login may not read the logs",
            404: "not found: is this Loki's address?",
            429: "Loki asks to wait: too many requests",
        }
        said = e.read(300).decode(errors="replace").strip() if e.code == 400 else ""  # such as a query's error
        raise LokiError(f"{why.get(e.code, f'HTTP {e.code}')}{': ' + said if said else ''} ({where})") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LokiError(f"no answer from {where}: {e}") from None
    if len(body) > MAX_ANSWER:
        raise LokiError(f"the answer of {where} is too big")
    try:
        return json.loads(body)
    except ValueError:
        raise LokiError(f"{where} did not answer as Loki does") from None


def get(loki: Loki, path: str, params: Mapping[str, str], fetch: Fetch = fetch_json) -> Any:
    """The `data` of Loki's answer on /loki/api/v1/<path>."""
    found = fetch(f"{loki.url.rstrip('/')}/loki/api/v1/{path}?{urllib.parse.urlencode(params)}", loki.headers())
    if not isinstance(found, dict) or found.get("status") != "success":
        raise LokiError(f"{loki.url} did not answer as Loki does")
    return found.get("data")


def labels(loki: Loki, now: float, fetch: Fetch = fetch_json) -> list[str]:
    """The labels of the streams that Loki saw in the last days. It raises LokiError when the connection does not
    work, so it is also the check of a new connection."""
    found = get(loki, "labels", {"start": _ns(now - LIST_DAYS * DAY), "end": _ns(now)}, fetch)
    return sorted(str(n) for n in found or [] if LABEL_NAME.fullmatch(str(n)) and not str(n).startswith("__"))


def label_for(store: Store, found: Iterable[str]) -> str:
    """The label that names a source: the one of the owner's choices, else the first of NAMES that Loki has; ""
    for none."""
    have = set(found)
    chosen_label = store.setting(LABEL)
    if chosen_label in have:
        return chosen_label
    return next((n for n in NAMES if n in have), "")


def sources(loki: Loki, label: str, now: float, fetch: Fetch = fetch_json) -> list[dict[str, Any]]:
    """The sources that Loki saw in the last days: the values of `label`, each with what its streams say of it for
    the suggestions (its Compose project, an image) and the hosts where it runs. Never lastcall's own steps."""
    if not LABEL_NAME.fullmatch(label):
        raise LokiError(f"not a label: {label!r}")
    params = {"match[]": f'{{{label}=~".+"}}', "start": _ns(now - LIST_DAYS * DAY), "end": _ns(now)}
    rows: dict[str, dict[str, Any]] = {}
    for series in get(loki, "series", params, fetch) or []:
        name = str(series.get(label) or "") if isinstance(series, dict) else ""
        if not SOURCE.fullmatch(name) or name == UNKNOWN or name.startswith(STEP):
            continue
        row = rows.setdefault(name, {"name": name, "image": "", "compose_project": "", "hosts": set()})
        row["image"] = row["image"] or str(series.get("image") or "")
        row["compose_project"] = row["compose_project"] or str(series.get("compose_project") or "")
        if series.get("host"):
            row["hosts"].add(str(series["host"]))
    listed = sorted(rows.values(), key=lambda r: str(r["name"]))[:MAX_SOURCES]
    return [{**r, "hosts": sorted(r["hosts"])} for r in listed]


def chosen(store: Store) -> dict[str, str]:
    """The project of each source, as the owner chose it: "" for a source that the owner said no to."""
    return logs.chosen(store, CHOSEN)


def choose(store: Store, choices: Mapping[str, str]) -> None:
    logs.choose(store, choices, CHOSEN)


def names_for(repo: str, choices: Mapping[str, str]) -> list[str]:
    """The sources of a project."""
    return sorted(n for n, p in choices.items() if p and p.lower() == repo.lower())[: logs.MAX_CONTAINERS]


def query(label: str, value: str) -> str:
    """The LogQL that reads the lines of a source that may be part of an error."""
    return f"{{{label}={json.dumps(value)}}} |~ `{FILTER}`"


def read(
    loki: Loki, store: Store, repo: str, files: Iterable[str], now: float, fetch: Fetch = fetch_json
) -> tuple[list[logs.Container], list[logs.Kind]]:
    """The sources of a project, and the kinds of error in their logs of the last day."""
    label, names = store.setting(LABEL), names_for(repo, chosen(store))
    if not LABEL_NAME.fullmatch(label) or not names:
        return [], []
    found = []
    for name in names:
        params = {
            "query": query(label, name),
            "start": _ns(now - logs.HOURS * 3600),
            "end": _ns(now),
            "limit": str(LIMIT),
            "direction": "backward",  # the newest lines
        }
        found.append({"name": name, "streams": streams(get(loki, "query_range", params, fetch))})
    return logs.errors(found, files)


def streams(data: Any) -> list[list[str]]:
    """The lines of each stream of Loki's answer, the oldest first, each after its time as Docker writes it, which
    `lastcall.logs` reads. The pieces of one stream (Loki shards a busy one, and an older Loki splits it by each
    line's level) come together again, by time."""
    groups: dict[tuple[tuple[str, str], ...], list[tuple[int, str]]] = {}
    for stream in (data.get("result") if isinstance(data, dict) else None) or []:
        if not isinstance(stream, dict):
            continue
        tags = stream.get("stream")
        pairs = tags.items() if isinstance(tags, dict) else ()
        key = tuple(sorted((str(k), str(v)) for k, v in pairs if k not in METADATA))
        entries = groups.setdefault(key, [])
        for value in reversed(stream.get("values") or []):  # Loki gives the newest first
            if isinstance(value, list) and len(value) >= 2 and str(value[0]).isdigit():
                entries.append((int(value[0]), str(value[1])))
    out = []
    for entries in groups.values():
        entries.sort(key=lambda e: e[0])  # stable: lines of the same time keep their order
        lines = []
        for when, text in entries:
            stamp = (
                datetime.fromtimestamp(when // 10**9, UTC).strftime("%Y-%m-%dT%H:%M:%S") + f".{when % 10**9:09d}Z "
            )
            lines += [stamp + part[:MAX_LINE] for part in text.splitlines() or [""]]
        out.append(lines)
    return out


def section(containers: list[logs.Container], kinds: list[logs.Kind], label: str) -> str:
    """The pack's section: the sources, and the errors in their logs."""
    return logs.section(containers, kinds, f"Sources in Loki, by their label {label}")


def _ns(when: float) -> str:
    return str(int(when * 10**9))
