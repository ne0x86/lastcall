"""Run one step as a local subprocess. For tests and development only.

In production every step runs in a sandboxed runner container that the supervisor starts (DESIGN.md, section 5).
This runner applies none of that isolation and ignores the invocation's files and mounts: use it with the scripted
engine, or on a disposable machine.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import replace

from lastcall.engines.base import Engine, Event, StepResult, StepSpec

STEP_TIMEOUT = 45 * 60.0


def run_local(engine: Engine, step: StepSpec, timeout: float = STEP_TIMEOUT) -> StepResult:
    inv = engine.invocation(step)
    proc = subprocess.Popen(
        list(inv.argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **inv.env},
    )
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    assert stdin is not None and stdout is not None and stderr is not None

    def feed() -> None:
        try:
            stdin.write(inv.stdin)
            stdin.close()
        except OSError:  # the process ended before reading everything
            pass

    tail: list[str] = []

    def drain() -> None:  # stderr in its own thread: a full pipe would block the CLI
        for line in stderr:
            tail.append(line)
            del tail[:-50]

    timed_out = threading.Event()

    def kill() -> None:
        timed_out.set()
        proc.kill()

    threads = [threading.Thread(target=feed, daemon=True), threading.Thread(target=drain, daemon=True)]
    for t in threads:
        t.start()
    timer = threading.Timer(timeout, kill)
    timer.start()
    events: list[Event] = []
    try:
        for line in stdout:
            ev = engine.parse(line)
            if ev is not None:
                events.append(ev)
        proc.wait()
    finally:
        timer.cancel()
    threads[1].join(timeout=5)

    result = engine.result(step, events)
    if timed_out.is_set():
        return replace(result, ok=False, output=None, error=f"timed out after {timeout:g} s")
    if not result.ok and result.error is None:
        return replace(result, error="".join(tail)[-1500:].strip() or f"exit code {proc.returncode}")
    return result
