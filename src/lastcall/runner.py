"""Run one step as a local subprocess. For tests and development only.

In production every step runs in the sandbox through the supervisor (`lastcall.sandbox`, DESIGN.md section 5).
This runner applies none of that isolation and ignores the invocation's files and mounts: use it with the scripted
engine, or on a disposable machine.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace

from lastcall.engines.base import Engine, Event, StepResult, StepSpec
from lastcall.process import Outcome, stream

STEP_TIMEOUT = 45 * 60.0


def run_local(engine: Engine, step: StepSpec, timeout: float = STEP_TIMEOUT) -> StepResult:
    inv = engine.invocation(step)
    events: list[Event] = []

    def on_line(line: str) -> bool:
        ev = engine.parse(line)
        if ev is not None:
            events.append(ev)
        return True

    outcome = stream(inv.argv, inv.stdin, timeout, on_line, env={**os.environ, **inv.env})
    return finish(engine, step, events, outcome, timeout)


def finish(engine: Engine, step: StepSpec, events: Sequence[Event], outcome: Outcome, timeout: float) -> StepResult:
    """The engine's result, corrected with what only the process knows: a timeout, or why it failed."""
    result = engine.result(step, events)
    if outcome.timed_out:
        return replace(result, ok=False, output=None, error=f"timed out after {timeout:g} s")
    if not result.ok and result.error is None:
        return replace(result, error=outcome.stderr_tail.strip() or f"exit code {outcome.exit_code}")
    return result
