"""Errors in the logs of a project's containers (DESIGN.md, section 8), for apps that run in Docker on lastcall's own
host, with no change to their code. The owner chooses the project of each container on the settings page, where
lastcall suggests one from the container's image (`ghcr.io/<owner>/<name>`), its Compose project or its name; or
labels it `lastcall.project=<owner/name>` in its compose file. lastcall reads no container until then. The supervisor
lists the containers and reads their logs (`lastcall.supervisor`).

This module turns their lines into errors grouped by kind: a Python traceback by its exception and the place in the
code where it happened, a stack of Node.js or Java by its first line and frames, and any other line with an error
level (ERROR, CRITICAL, FATAL, PANIC, `level=error`, `[error]`) by its text with numbers, ids and quoted values
taken out. It also says how each container is: restarts, and whether the kernel killed it for lack of memory.

What reaches a model is masked as a tracker's errors are (`lastcall.signals.mask`): one sample line of each kind,
how often and when, and the file and line of its frames.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from lastcall.signals import mask, repo_path

if TYPE_CHECKING:
    from lastcall.sandbox import Sandbox
    from lastcall.store import Store

LABEL = "lastcall.project"
CHOSEN = "container_projects"  # the owner's choices: container name -> owner/name, as JSON
HOURS = 24
MAX_CONTAINERS = 10  # per project
TITLE = f"Errors in the container logs (last {HOURS} hours)"
MAX_KINDS = 8
MAX_FRAMES = 6

STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?Z ")  # Docker's, before each line
TRACEBACK = "Traceback (most recent call last):"
CHAINED = ("During handling of the above exception", "The above exception was the direct cause")
PY_FRAME = re.compile(r'^\s+File "([^"]+)", line (\d+), in (.+)$')
JS_FRAME = re.compile(r"^\s+at (?:(.+?) \()?((?:/|[A-Za-z]:\\|\w+://)[^():]+):(\d+):\d+\)?$")
JAVA_FRAME = re.compile(r"^\s+at ([\w.$<>]+)\(([\w.]+):(\d+)\)$")
STARTS = re.compile(r"^(?:Uncaught )?[A-Za-z_][\w.]*(?:Error|Exception)\b[:\s]|^panic: |^Exception in thread ")
LEVEL = re.compile(r"\b(?:ERROR|CRITICAL|FATAL|PANIC|SEVERE|EMERG)\b")
STRUCTURED = re.compile(
    r"""\blevel["']?\s*[=:]\s*["']?(?:error|err|critical|crit|fatal|panic)\b|\[(?:error|crit|alert|emerg)\]""", re.I
)
# the date and time that an app prints at the start of its lines: "2026-10-26 09:52:53,001 ", "[2026/10/26 09:55:00] "
PREFIX = re.compile(
    r"^\[?(?:\d{4}[-/]\d{2}[-/]\d{2}[T ]?)?(?:\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?\]?\s+"
)


def chosen(store: Store) -> dict[str, str]:
    """The project of each container, as the owner chose it: "" for a container that the owner said no to, so that
    lastcall stops suggesting one for it."""
    try:
        found = json.loads(store.setting(CHOSEN) or "{}")
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in found.items()} if isinstance(found, dict) else {}


def choose(store: Store, choices: Mapping[str, str]) -> None:
    store.set_setting(CHOSEN, json.dumps(dict(sorted(choices.items()))))


def project_of(container: Mapping[str, Any], choices: Mapping[str, str]) -> str:
    """The project of a container: its label, or else the owner's choice; "" for none."""
    return str(container.get("label") or "") or choices.get(str(container.get("name") or ""), "")


def names_for(repo: str, listed: Iterable[Mapping[str, Any]], choices: Mapping[str, str]) -> list[str]:
    """The containers of a project, by name."""
    return [str(c["name"]) for c in listed if project_of(c, choices).lower() == repo.lower()][:MAX_CONTAINERS]


def suggest(container: Mapping[str, Any], repos: Iterable[str]) -> str:
    """The project that a container seems to belong to: by its image (`ghcr.io/<owner>/<name>:tag`), else its Compose
    project, else its name (`<name>-web-1`). "" when none fits, or when more than one does at the same step."""
    image = str(container.get("image") or "").lower().split("@")[0]
    if ":" in image.rsplit("/", 1)[-1]:  # a tag, not a registry's port
        image = image.rsplit(":", 1)[0]
    compose = str(container.get("compose_project") or "").lower()
    name = str(container.get("name") or "").lower()
    candidates = [(r, r.lower(), r.lower().split("/")[-1]) for r in repos]
    steps = (
        [repo for repo, full, _ in candidates if image == full or image.endswith("/" + full)],
        [repo for repo, _, short in candidates if compose == short],
        [repo for repo, _, short in candidates if name == short or name.startswith((short + "-", short + "_"))],
    )
    for found in steps:
        if found:
            return found[0] if len(found) == 1 else ""
    return ""


def read(box: Sandbox, store: Store, repo: str, files: Iterable[str] = ()) -> tuple[list[Container], list[Kind]]:
    """The containers of a project, and the kinds of error in their logs of the last day."""
    names = names_for(repo, box.containers(), chosen(store))
    return errors(box.logs(names, HOURS * 3600), files) if names else ([], [])


@dataclass(frozen=True)
class Container:
    name: str
    status: str
    restarts: int
    oom_killed: bool
    exit_code: int | None


@dataclass(frozen=True)
class Kind:
    container: str
    sample: str  # one line of it, masked
    count: int
    last: float | None
    frames: tuple[str, ...]  # file:line in function, the innermost last


def errors(found: Iterable[Mapping[str, Any]], files: Iterable[str] = ()) -> tuple[list[Container], list[Kind]]:
    """The containers and the kinds of error in their lines, the most frequent first."""
    known, containers = set(files), []
    groups: dict[tuple[str, str], list[tuple[float | None, str, tuple[str, ...]]]] = {}
    for c in found:
        name = str(c.get("name") or "?")
        code = c.get("exit_code")
        containers.append(
            Container(
                name,
                str(c.get("status") or ""),
                int(c.get("restarts") or 0),
                bool(c.get("oom_killed")),
                code if isinstance(code, int) else None,
            )
        )
        for stream in ("stdout", "stderr"):  # each in its own order: a traceback never spans the two
            for when, key, sample, frames in events([str(line) for line in c.get(stream) or []], known):
                groups.setdefault((name, key), []).append((when, sample, frames))
    kinds = []
    for (name, _), seen in groups.items():
        times = [t for t, _, _ in seen if t is not None]
        _, sample, frames = seen[-1]
        kinds.append(Kind(name, sample, len(seen), max(times) if times else None, frames))
    kinds.sort(key=lambda k: (-k.count, -(k.last or 0)))
    return containers, kinds[:MAX_KINDS]


def events(lines: Sequence[str], files: set[str]) -> list[tuple[float | None, str, str, tuple[str, ...]]]:
    """Each error in the lines: when, its kind, a sample line and the frames of its stack."""
    out = []
    i, n = 0, len(lines)
    while i < n:
        when, text = stamp(lines[i])
        if text.strip() == TRACEBACK:
            frames: list[tuple[str, str, str]] = []
            j = i + 1
            while j < n and ((rest := stamp(lines[j])[1]).startswith((" ", "\t")) or not rest.strip()):
                if found := PY_FRAME.match(rest):
                    frames.append((found[1], found[2], found[3]))
                j += 1
            raised = stamp(lines[j])[1].strip() if j < n else "an exception"
            k = j + 1
            while k < n and not stamp(lines[k])[1].strip():
                k += 1
            if k < n and stamp(lines[k])[1].startswith(CHAINED):  # the next traceback is the one that was raised
                i = k + 1
                continue
            place = _frames(frames, files)
            kind = raised.split(":", 1)[0] + (f" at {place[-1].split(' in ')[0]}" if place else "")
            out.append((when, kind, mask(raised)[:200], place))
            i = j + 1
            continue
        if STARTS.match(text) or LEVEL.search(text) or STRUCTURED.search(text):
            frames = []
            j = i + 1
            while j < n:
                rest = stamp(lines[j])[1]
                if found := JS_FRAME.match(rest):
                    frames.append((found[2], found[3], found[1] or "?"))
                elif found := JAVA_FRAME.match(rest):
                    frames.append((found[2], found[3], found[1]))
                elif not rest.startswith(("\t", "    at ")):
                    break
                j += 1
            frames.reverse()  # these stacks name the innermost frame first
            out.append(
                (when, signature(text), mask(PREFIX.sub("", text.strip(), count=1))[:200], _frames(frames, files))
            )
            i = j
            continue
        i += 1
    return out


def signature(text: str) -> str:
    """What makes two lines the same kind of error: the text without the app's own timestamp, with personal data,
    numbers, ids and quoted values taken out."""
    text = mask(PREFIX.sub("", text.strip(), count=1))
    text = re.sub(r"0x[0-9a-fA-F]+|\b[0-9a-fA-F]{8,}\b", "<hex>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", text)
    text = re.sub(r"\d+", "<n>", text)
    return re.sub(r"\s+", " ", text)[:160]


def stamp(line: str) -> tuple[float | None, str]:
    """Docker's timestamp of a line, and its text."""
    found = STAMP.match(line)
    if not found:
        return None, line
    when = datetime.strptime(found[1], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    return when, line[found.end() :]


def section(containers: Sequence[Container], kinds: Sequence[Kind]) -> str:
    """The pack's section: how the containers are, and the errors in their logs."""
    if not containers:
        return ""
    states = []
    for c in containers:
        notes = [c.status or "?"]
        if c.restarts:
            notes.append(f"restarted {c.restarts} times")
        if c.oom_killed:
            notes.append("the last stop was the kernel killing it for lack of memory")
        elif c.status == "exited" and c.exit_code:
            notes.append(f"exited with code {c.exit_code}")
        states.append(f"{c.name} ({'; '.join(notes)})")
    lines = [f"Containers: {', '.join(states)}."]
    if not kinds:
        lines.append(f"No errors in their logs of the last {HOURS} hours.")
        return "\n".join(lines)
    lines.append(f"{len(kinds)} kinds of errors in the last {HOURS} hours, the most frequent first (lines masked):")
    for k in kinds:
        last = datetime.fromtimestamp(k.last, UTC).strftime("%Y-%m-%d %H:%M UTC") if k.last else "?"
        often = "once" if k.count == 1 else f"{k.count} times"
        lines.append(f"- [{k.container}] {k.sample} ({often}, last {last})")
        lines += [f"    {frame}" for frame in k.frames]
    return "\n".join(lines)


def _frames(frames: Sequence[tuple[str, str, str]], files: set[str]) -> tuple[str, ...]:
    """The last frames, the innermost last, with the repository's paths; those of the repository when it has some."""
    own = [f for f in frames if repo_path(f[0], files) in files] or list(frames)
    return tuple(mask(f"{repo_path(path, files)}:{line} in {function}") for path, line, function in own[-MAX_FRAMES:])
