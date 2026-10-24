"""The sandbox against a running supervisor, egress proxy and runner image (compose.yaml).

Run them with `docker compose --profile test run --rm it`: the test container plays the service's part.
"""

import os
import shutil
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from lastcall.engines.base import Event, EventKind, Invocation, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.sandbox import Sandbox, SandboxError

SOCKET = os.environ.get("LASTCALL_SOCKET")
DATA = Path(os.environ.get("LASTCALL_DATA_ROOT", "/data"))
SANDBOX = Sandbox(Path(SOCKET or "/nonexistent"), DATA)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not SOCKET, reason="needs the sandbox: docker compose --profile test run --rm it"),
]


class Shell:
    """Runs a shell script in the runner image. The engine name only selects the image."""

    name: str = "claude-code"
    capabilities: frozenset[str] = frozenset()
    egress: tuple[str, ...] = ()
    home: str = "/home/runner/.claude"

    def __init__(
        self, script: str, files: Mapping[str, str] | None = None, mounts: Mapping[str, Path] | None = None
    ) -> None:
        self.script, self.files, self.mounts = script, files or {}, mounts or {}

    def invocation(self, step: StepSpec) -> Invocation:
        return Invocation(("sh", "-c", self.script), files=self.files, mounts=self.mounts)

    def parse(self, line: str) -> Event | None:
        return Event(EventKind.MESSAGE, {"text": line.rstrip("\n")})

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        return StepResult(True, "\n".join(str(e.data["text"]) for e in events))


@pytest.fixture
def workspace() -> Iterator[Path]:
    run = DATA / "runs" / f"it-{uuid.uuid4().hex[:8]}"
    (run / "src").mkdir(parents=True)
    (run / "src" / "hello.txt").write_text("hello\n", encoding="utf-8")
    yield run / "src"
    shutil.rmtree(run, ignore_errors=True)


def sh(
    script: str,
    workspace: Path,
    *,
    writable: bool = False,
    account: str | None = None,
    timeout: float = 60.0,
    files: Mapping[str, str] | None = None,
    mounts: Mapping[str, Path] | None = None,
) -> StepResult:
    step = StepSpec("test", "none", "", writable=writable)
    return SANDBOX.run(
        Shell(script, files, mounts), step, workspace=workspace, account_volume=account, timeout=timeout
    )


def test_runs_unprivileged(workspace: Path) -> None:
    out = sh("id -u; grep CapEff /proc/self/status", workspace).output
    assert out.splitlines() == ["1000", "CapEff:\t0000000000000000"]


def test_reviewer_workspace_is_read_only(workspace: Path) -> None:
    out = sh("cat /work/hello.txt; touch /work/x 2>/dev/null && echo WROTE || echo READ_ONLY", workspace).output
    assert out.splitlines() == ["hello", "READ_ONLY"]


def test_implementer_workspace_is_writable(workspace: Path) -> None:
    assert sh("touch /work/new.txt && echo WROTE", workspace, writable=True).output == "WROTE"
    assert (workspace / "new.txt").exists()


def test_step_files_are_read_only(workspace: Path) -> None:
    shared = workspace.parent / "shared.md"
    shared.write_text("shared context\n", encoding="utf-8")
    script = "cat /ctx/schema.json /ctx/shared.md; touch /ctx/x 2>/dev/null && echo CTX_RW || echo CTX_RO"
    out = sh(script, workspace, files={"/ctx/schema.json": "{}\n"}, mounts={"/ctx/shared.md": shared}).output
    assert out.splitlines() == ["{}", "shared context", "CTX_RO"]


def test_egress_reaches_the_model_api(workspace: Path) -> None:
    out = sh("curl -s -o /dev/null -w '%{http_code}' --max-time 20 https://api.anthropic.com/", workspace).output
    assert out not in ("", "000")


def test_egress_refuses_other_domains(workspace: Path) -> None:
    script = "curl -s -o /dev/null -w '%{http_connect}' --max-time 20 https://example.com/; echo \" rc=$?\""
    out = sh(script, workspace).output
    assert out.startswith("403")  # the proxy refused the tunnel
    assert not out.endswith("rc=0")


def test_no_way_around_the_proxy(workspace: Path) -> None:
    out = sh("curl -s -o /dev/null --noproxy '*' --max-time 10 https://api.anthropic.com/; echo rc=$?", workspace)
    assert out.output != "rc=0"


def test_account_volume_is_the_cli_home(workspace: Path) -> None:
    out = sh("touch /home/runner/.claude/it && echo HOME_RW", workspace, account="lastcall-account-it").output
    assert out == "HOME_RW"


def test_timeout_stops_the_container(workspace: Path) -> None:
    res = sh("sleep 30", workspace, timeout=2)
    assert not res.ok
    assert res.error == "timed out after 2 s"


def test_scripted_engine_through_the_sandbox(workspace: Path) -> None:
    class InRunner(Scripted):
        name = "codex"

        def invocation(self, step: StepSpec) -> Invocation:
            return replace(super().invocation(step), argv=("cat",))

    engine = InRunner({"judge": [{"kind": "tool"}, {"kind": "result", "ok": True, "output": {"verdicts": []}}]})
    res = SANDBOX.run(engine, StepSpec("judge", "none", "", schema={"type": "object"}), workspace=workspace)
    assert res.ok
    assert res.output == {"verdicts": []}
    assert res.tool_uses == 1


def test_supervisor_refuses_a_workspace_outside_runs(workspace: Path) -> None:
    with pytest.raises(SandboxError, match="inside runs/"):
        SANDBOX.run(Shell("true"), StepSpec("test", "none", ""), workspace=DATA / "steps")
