"""Run a step in the sandbox: the service's side of the supervisor protocol (`lastcall.supervisor`)."""

from __future__ import annotations

import http.client
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lastcall.engines.base import Engine, Event, Invocation, StepResult, StepSpec
from lastcall.process import Outcome
from lastcall.runner import STEP_TIMEOUT, finish


class SandboxError(RuntimeError):
    """The supervisor refused the step or could not be reached."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


@dataclass(frozen=True)
class Sandbox:
    socket_path: Path
    data_root: Path  # where this process sees the data volume

    def run(
        self,
        engine: Engine,
        step: StepSpec,
        *,
        workspace: Path,
        account_volume: str | None = None,
        timeout: float = STEP_TIMEOUT,
    ) -> StepResult:
        """One agent step: the engine builds the command and reads its events."""
        events: list[Event] = []

        def on_line(line: str) -> None:
            ev = engine.parse(line)
            if ev is not None:
                events.append(ev)

        outcome = self.call(
            engine.name,
            engine.invocation(step),
            on_line,
            workspace=workspace,
            account_volume=account_volume,
            home=engine.home if account_volume else None,
            writable=step.writable,
            timeout=timeout,
        )
        return finish(engine, step, events, outcome, timeout)

    def call(
        self,
        engine: str,
        inv: Invocation,
        on_line: Callable[[str], None],
        *,
        workspace: Path,
        account_volume: str | None = None,
        home: str | None = None,
        writable: bool = False,
        timeout: float = STEP_TIMEOUT,
    ) -> Outcome:
        """Run any invocation in the runner image of `engine` and pass on each line it prints."""
        body = {
            "engine": engine,
            "argv": list(inv.argv),
            "stdin": inv.stdin,
            "env": dict(inv.env),
            "files": dict(inv.files),
            "copies": {dest: self._relative(src) for dest, src in inv.mounts.items()},
            "workspace": self._relative(workspace),
            "writable": writable,
            "account_volume": account_volume,
            "home": home,
            "timeout": timeout,
        }
        final: dict[str, Any] = {}
        conn = _UnixConnection(str(self.socket_path), timeout + 120)
        try:
            conn.request("POST", "/steps", json.dumps(body), {"Content-Type": "application/json"})
            resp = conn.getresponse()
            if resp.status != 200:
                raise SandboxError(_error(resp.read()))
            for raw in resp:
                frame = json.loads(raw)
                if "out" in frame:
                    on_line(str(frame["out"]))
                else:
                    final = frame
        except OSError as e:
            raise SandboxError(f"supervisor unreachable at {self.socket_path}: {e}") from e
        finally:
            conn.close()
        return Outcome(int(final.get("exit", -1)), bool(final.get("timed_out")), str(final.get("stderr") or ""))

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.data_root.resolve()).as_posix()
        except ValueError:
            raise SandboxError(f"{path} is outside the data volume {self.data_root}") from None


def _error(raw: bytes) -> str:
    try:
        return str(json.loads(raw).get("error"))
    except (ValueError, AttributeError):
        return raw.decode(errors="replace")[:300]
