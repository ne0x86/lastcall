"""The sandbox against a running supervisor, egress proxy and runner image (compose.yaml).

Run them with `docker compose --profile test run --rm it`: the test container plays the service's part.
"""

import os
import shutil
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from lastcall.accounts import Account, read_quota
from lastcall.engines.base import Event, EventKind, Invocation, StepResult, StepSpec
from lastcall.engines.opencode import OpenCode
from lastcall.engines.scripted import Scripted
from lastcall.quota import QuotaError
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
    title: str = "Shell"
    provider: str = "tests"
    capabilities: frozenset[str] = frozenset()
    egress: tuple[str, ...] = ()
    home: str = "/home/runner/.claude"
    home_env: str = "CLAUDE_CONFIG_DIR"
    login: tuple[str, ...] = ()

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


@pytest.mark.parametrize(
    "url",
    ["https://proxy.golang.org/", "https://index.crates.io/config.json", "https://repo.maven.apache.org/maven2/"],
)
def test_egress_reaches_the_package_registries(workspace: Path, url: str) -> None:
    out = sh("curl -s -o /dev/null -w '%{http_code}' --max-time 20 " + url, workspace).output
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


@pytest.mark.parametrize("engine", ["claude-code", "codex"])
def test_quota_probe_without_login_fails_cleanly(engine: str) -> None:
    """The real CLI, through the proxy, with an empty account volume: an error, not a hang or a wrong reading."""
    with pytest.raises(QuotaError):
        read_quota(Account(f"it-nologin-{engine}", engine), SANDBOX, time.time())


def test_scanners_find_what_they_should_in_the_runner(workspace: Path) -> None:
    """The real scanners, in the runner, read-only on a clone: a secret in the history, a vulnerable dependency,
    a shell=True and an untagged base image."""
    import subprocess

    from lastcall.scanners import TITLES, scan

    clone = workspace.parent / "clone"
    clone.mkdir()
    env = dict(
        os.environ,
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        HOME=str(workspace.parent),
    )

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(clone), *args], check=True, capture_output=True, env=env)

    git("init", "-q", "-b", "main")
    token = "ghp_" + "Zq8RkW2mXv5LtY9pNb3HcJ7dGf4AsE6uQi1o"
    (clone / "config.py").write_text(f'TOKEN = "{token}"\n', encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "config")
    (clone / "config.py").write_text("import os\nTOKEN = os.environ['TOKEN']\n", encoding="utf-8")
    (clone / "requirements.txt").write_text("requests==2.19.0\n", encoding="utf-8")
    (clone / "Dockerfile").write_text('FROM python\nRUN pip install flask "urllib3==1.24.1"\n', encoding="utf-8")
    (clone / "app.py").write_text(
        "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n", encoding="utf-8"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "fixes")

    found = scan(SANDBOX, "claude-code", clone)
    assert "github-pat" in found[TITLES["gitleaks"]]  # only in the history: gitleaks reads it
    assert "requests 2.19.0" in found[TITLES["osv-scanner"]]
    assert "urllib3 1.24.1" in found[TITLES["osv-scanner"]]  # pinned only in the Dockerfile
    assert "subprocess-shell-true" in found[TITLES["semgrep"]]
    assert "DL3006" in found[TITLES["hadolint"]]


def test_a_check_runs_in_the_projects_image(workspace: Path) -> None:
    """A verify step may name the project's image instead of the runner's, with the same isolation."""
    lines: list[str] = []
    script = "id -u; cat /work/hello.txt; grep CapEff /proc/self/status"
    outcome = SANDBOX.call(
        "claude-code", Invocation(("sh", "-c", script)), lines.append, workspace=workspace, image="busybox:1.36.1"
    )
    assert outcome.exit_code == 0
    assert lines == ["1000", "hello", "CapEff:\t0000000000000000"]


class OpenCodeOnAStandIn(OpenCode):
    """The runner's real opencode, on a local model whose server is a stand-in that the step starts first."""

    def invocation(self, step: StepSpec) -> Invocation:
        inv = super().invocation(step)
        start = (
            "python3 /ctx/stand_in.py 18765 >/dev/null 2>&1 & "
            'until [ -e /tmp/stand-in-ready ]; do sleep 0.1; done; exec "$@"'
        )
        stand_in = (Path(__file__).parent / "stand_in_provider.py").read_text(encoding="utf-8")
        return replace(
            inv, argv=("sh", "-c", start, "step", *inv.argv), files={**inv.files, "/ctx/stand_in.py": stand_in}
        )


def test_opencode_runs_a_review_in_the_runner(workspace: Path) -> None:
    """opencode of the runner image, configured only through its environment and with the proxy settings of a step,
    reads a file with its tool and answers with the JSON that the schema asks for. The model server is not behind
    the proxy, as the ollama service is not."""
    engine = OpenCodeOnAStandIn("ollama", base_url="http://127.0.0.1:18765/v1")
    schema = {"type": "object", "required": ["summary", "findings"]}
    res = SANDBOX.run(
        engine,
        StepSpec("reviewer:bugs", "stand-in", "Review hello.txt.", schema=schema),
        workspace=workspace,
        timeout=300,
    )
    assert res.ok, res.error
    assert res.output == {"summary": "nothing to report", "findings": []}
    assert res.tool_uses == 1
    assert res.usage == {"input": 2468, "output": 112, "cache_read": 0}


def test_the_deps_probe_reads_the_latest_versions_through_the_proxy(workspace: Path) -> None:
    """osv-scanner in the runner lists the dependencies, and deps.dev, through the egress proxy, says how far
    behind they are."""
    from lastcall.scanners import TITLES, VERSIONS, dependencies

    (workspace / "requirements.txt").write_text("requests==2.19.0\n", encoding="utf-8")
    found = dependencies(SANDBOX, "claude-code", workspace)
    assert "- requests 2.19.0 → " in found[VERSIONS]
    assert "requests 2.19.0: " in found[TITLES["osv-scanner"]]  # it has known vulnerabilities
