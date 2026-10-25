import sys
from collections.abc import Sequence

from lastcall.engines.base import Event, Invocation, StepResult, StepSpec
from lastcall.engines.scripted import Scripted
from lastcall.runner import run_local

JUDGE = StepSpec("judge", "none", "Judge the findings.", schema={"type": "object"})


def test_scripted_step_runs_end_to_end() -> None:
    engine = Scripted(
        {"judge": [{"kind": "tool", "count": 2}, {"kind": "result", "ok": True, "output": {"verdicts": []}}]}
    )
    res = run_local(engine, JUDGE)
    assert res.ok
    assert res.output == {"verdicts": []}
    assert res.tool_uses == 2


def test_step_without_result_fails() -> None:
    res = run_local(Scripted({"judge": [{"kind": "tool"}]}), JUDGE)
    assert not res.ok
    assert res.error == "no result"


class Sleeper:
    name: str = "sleeper"
    title: str = "Sleeper"
    provider: str = "tests"
    capabilities: frozenset[str] = frozenset()
    egress: tuple[str, ...] = ()
    home: str = "/tmp"
    home_env: str = "HOME"
    login: tuple[str, ...] = ()

    def invocation(self, step: StepSpec) -> Invocation:
        return Invocation((sys.executable, "-c", "import time; time.sleep(30)"))

    def parse(self, line: str) -> Event | None:
        return None

    def result(self, step: StepSpec, events: Sequence[Event]) -> StepResult:
        return StepResult(False)


def test_timeout_kills_the_step() -> None:
    res = run_local(Sleeper(), JUDGE, timeout=0.5)
    assert not res.ok
    assert res.error == "timed out after 0.5 s"
