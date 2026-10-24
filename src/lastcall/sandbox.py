"""Run a step in the sandbox: the service's side of the supervisor protocol (`lastcall.supervisor`)."""

from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lastcall.engines.base import Engine, Event, StepResult, StepSpec
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
        inv = engine.invocation(step)
        body = {
            "engine": engine.name,
            "argv": list(inv.argv),
            "stdin": inv.stdin,
            "env": dict(inv.env),
            "files": dict(inv.files),
            "copies": {dest: self._relative(src) for dest, src in inv.mounts.items()},
            "workspace": self._relative(workspace),
            "writable": step.writable,
            "account_volume": account_volume,
            "home": engine.home if account_volume else None,
            "timeout": timeout,
        }
        events: list[Event] = []
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
                    ev = engine.parse(frame["out"])
                    if ev is not None:
                        events.append(ev)
                else:
                    final = frame
        except OSError as e:
            raise SandboxError(f"supervisor unreachable at {self.socket_path}: {e}") from e
        finally:
            conn.close()
        outcome = Outcome(int(final.get("exit", -1)), bool(final.get("timed_out")), str(final.get("stderr") or ""))
        return finish(engine, step, events, outcome, timeout)

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
