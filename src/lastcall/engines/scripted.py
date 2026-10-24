"""A scripted engine that replays canned events, so recipes run end to end in tests and in CI without quota.

The script maps a step role to the events the engine prints, one JSON object per line:

    {"kind": "tool", "count": 2}
    {"kind": "result", "ok": true, "output": {"findings": []}}

`python -m lastcall.engines.scripted` copies stdin to stdout, so the runner starts it like any other CLI.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import Any

from lastcall.engines.base import Event, EventKind, Invocation, StepResult, StepSpec, count_tools, json_line, last


class Scripted:
    name: str = "scripted"
    capabilities: frozenset[str] = frozenset({"json_schema"})
    egress: tuple[str, ...] = ()
    home: str = "/home/runner"
    home_env: str = "HOME"
    login: tuple[str, ...] = ()

    def __init__(self, script: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        self.script = script

    def invocation(self, step: StepSpec) -> Invocation:
        if step.role not in self.script:
            raise KeyError(f"no script for role {step.role!r}")
        stdin = "".join(json.dumps(ev) + "\n" for ev in self.script[step.role])
        return Invocation((sys.executable, "-m", "lastcall.engines.scripted"), stdin=stdin)

    def parse(self, line: str) -> Event | None:
        ev = json_line(line)
        if ev is None:
            return None
        try:
            kind = EventKind(ev.get("kind"))
        except ValueError:
            return None
        return Event(kind, {k: v for k, v in ev.items() if k != "kind"})

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        res = last(events, EventKind.RESULT) or {}
        output = res.get("output")
        ok = bool(res.get("ok")) and (step.schema is None or output is not None)
        return StepResult(
            ok,
            output if ok else None,
            None if ok else str(res.get("error") or "no result"),
            tool_uses=count_tools(events),
            turns=res.get("turns"),
            cost_usd=res.get("cost_usd"),
            usage=res.get("usage"),
        )


def main() -> None:
    for line in sys.stdin:
        sys.stdout.write(line)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
