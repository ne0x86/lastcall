"""Run a process: feed its stdin, stream its stdout line by line, keep the end of its stderr, enforce a timeout."""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    exit_code: int
    timed_out: bool
    stderr_tail: str


def stream(
    argv: Sequence[str],
    stdin: str,
    timeout: float,
    on_line: Callable[[str], bool],
    *,
    env: Mapping[str, str] | None = None,
    stop: Callable[[], None] | None = None,
) -> Outcome:
    """Run `argv` and call `on_line` for every line it prints. When `on_line` returns False or the timeout
    expires, `stop` ends the process (by default it is killed)."""
    proc = subprocess.Popen(
        list(argv),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(env) if env is not None else None,
    )
    pin, pout, perr = proc.stdin, proc.stdout, proc.stderr
    assert pin is not None and pout is not None and perr is not None
    end = stop or proc.kill

    def feed() -> None:
        try:
            pin.write(stdin)
            pin.close()
        except OSError:  # the process ended before reading everything
            pass

    tail: list[str] = []

    def drain() -> None:  # stderr in its own thread: a full pipe would block the process
        for line in perr:
            tail.append(line)
            del tail[:-50]

    timed_out = threading.Event()

    def expire() -> None:
        timed_out.set()
        end()

    feeder = threading.Thread(target=feed, daemon=True)
    drainer = threading.Thread(target=drain, daemon=True)
    feeder.start()
    drainer.start()
    timer = threading.Timer(timeout, expire)
    timer.start()
    try:
        for line in pout:
            if not on_line(line):
                end()
                break
        proc.wait()
    finally:
        timer.cancel()
    drainer.join(timeout=5)
    return Outcome(proc.returncode, timed_out.is_set(), "".join(tail)[-1500:])
