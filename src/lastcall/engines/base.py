"""The engine contract: an agent CLI that runs one step inside a runner container. See DESIGN.md, section 7.

An engine only builds the command and reads its output. Where the command runs is the supervisor's job: it starts
the sandboxed runner (read-only mount for reviewers, egress proxy). The container is the sandbox, so an engine can
run its CLI in the CLI's non-interactive, fully automatic mode.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from lastcall.quota import Reading

WORKDIR = "/work"  # the repository inside the runner
CONTEXT_DIR = "/ctx"  # read-only files for the step: shared context, schema


@dataclass(frozen=True)
class StepSpec:
    role: str  # "reviewer:bugs", "judge", "implementer"...
    model: str
    prompt: str
    effort: str | None = None
    shared_context: Path | None = None  # file shared by the steps of a run; providers cache it
    schema: Mapping[str, Any] | None = None  # JSON schema that the output must match
    writable: bool = False  # only implementers write to the workspace


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    files: Mapping[str, str] = field(default_factory=dict)  # container path -> content, mounted read-only
    mounts: Mapping[str, Path] = field(default_factory=dict)  # container path -> host file, mounted read-only


class EventKind(enum.Enum):
    TOOL = "tool"  # the agent used tools; data["count"]
    MESSAGE = "message"  # text from the agent; data["text"]
    USAGE = "usage"  # token counts; data["usage"]
    RATE_LIMIT = "rate_limit"  # rate-limit state reported by the provider; data["info"]
    RESULT = "result"  # end of the step
    ERROR = "error"  # data["detail"]


@dataclass(frozen=True)
class Event:
    kind: EventKind
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepResult:
    ok: bool
    output: Any = None  # the parsed JSON when the step has a schema, else the final text
    error: str | None = None
    tool_uses: int = 0
    turns: int | None = None
    cost_usd: float | None = None  # equivalent cost reported by the CLI, if any
    usage: Any = None  # token counts as the CLI reports them
    rate_limits: Any = None  # the last rate-limit state seen


class Engine(Protocol):
    name: str  # the id in accounts, settings and commands: "claude-code"
    title: str  # the CLI as people know it: "Claude Code"
    provider: str  # whose plans or keys it spends: "Claude"
    capabilities: frozenset[str]  # "json_schema", "system_prompt_file", "effort"
    egress: tuple[str, ...]  # domains the CLI needs, for the egress allowlist; "*.x.com" adds subdomains
    home: str  # where the CLI keeps its login; the account's credential volume is mounted here
    home_env: str  # the environment variable that points the CLI at `home`
    login: tuple[str, ...]  # the CLI's own interactive login, run inside the sandbox; empty: an API key (Keyed)

    def invocation(self, step: StepSpec) -> Invocation: ...

    def parse(self, line: str) -> Event | None: ...

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult: ...


@runtime_checkable
class QuotaProbe(Protocol):
    """An engine that can read its account's quota without calling the model."""

    def quota_invocation(self) -> Invocation: ...

    def quota_reading(self, account: str, lines: Sequence[str], now: float) -> Reading: ...


@dataclass(frozen=True)
class Provider:
    """What a keyed engine spends: a provider's API, or a model server on the owner's machine."""

    name: str  # the id in accounts and commands: "openrouter"
    title: str  # as people know it: "OpenRouter"
    keys: str  # where the owner creates an API key; "" for a local model server, which needs none
    models: str  # where the owner finds the names of its models
    catalog: str = ""  # its id in models.dev, whose catalog lists its models; "" when there is none to ask


@runtime_checkable
class Keyed(Protocol):
    """An engine that runs on an API key that lastcall holds, or on a local model, rather than on a CLI login."""

    providers: tuple[Provider, ...]

    def with_key(self, provider: str, key: str | None, base_url: str | None = None) -> Engine: ...


def probe_source(name: str) -> str:
    """A probe script from lastcall.probes, sent to the runner as a /ctx file (lastcall is not installed there)."""
    return resources.files("lastcall.probes").joinpath(f"{name}.py").read_text(encoding="utf-8")


def json_line(line: str) -> dict[str, Any] | None:
    """One JSON object per line; anything else (warnings, progress) is ignored."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def count_tools(events: Sequence[Event]) -> int:
    return sum(int(e.data.get("count", 1)) for e in events if e.kind is EventKind.TOOL)


def last(events: Sequence[Event], kind: EventKind) -> Mapping[str, Any] | None:
    return next((e.data for e in reversed(events) if e.kind is kind), None)
