"""How every recipe runs its agent steps: the quota gate before each one, and a record of what it cost."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from lastcall.engines.base import Engine, StepResult, StepSpec
from lastcall.quota import Gate, Reading
from lastcall.recipes import schema

StepRunner = Callable[[Engine, StepSpec, Path], StepResult]  # (engine, step, workspace)
QuotaCheck = Callable[[Sequence[str]], tuple[Gate, Reading | None]]  # the gate for the models of the next steps


@dataclass
class StepRecord:
    role: str
    model: str
    effort: str | None
    ok: bool
    seconds: float
    tool_uses: int
    cost_usd: float | None
    error: str | None
    quota_before: dict[str, float] | None  # window name -> % used, to measure what each step costs


class Tracked(Protocol):
    """What a recipe's report keeps of its steps: their records, and why the run stopped."""

    status: str
    stopped: str | None
    steps: list[StepRecord]
    notes: list[str]


@dataclass
class Steps:
    """Runs the agent steps of a report: the gate before each one, and a record of what it cost."""

    report: Tracked
    engine: Engine
    tiers: Mapping[str, tuple[str, str | None]]
    run_step: StepRunner
    check_quota: QuotaCheck
    shared: Path
    on_step: Callable[[StepRecord], None] | None = None
    peer: str = ""  # a peer's closed gate stops that peer, not the run

    def run(
        self,
        role: str,
        tier: str,
        text: str,
        schema_name: str,
        workspace: Path,
        writable: bool = False,
        then: Sequence[str] = (),
    ) -> StepResult | None:
        """Run one step behind the gate for its model and for `then`, the models of the steps that come after it:
        a run whose later steps could not start stops before it spends."""
        model, effort = self.tiers[tier]
        gate, reading = self.check_quota(list(dict.fromkeys([model, *then])))
        if not gate.ok:
            if self.peer:
                self.report.notes.append(f"{self.peer} stopped reviewing: {gate.reason}")
            else:
                self.report.status, self.report.stopped = "partial", gate.reason
            return None
        spec = StepSpec(role, model, text, effort, self.shared, schema(schema_name), writable)
        t0 = time.monotonic()
        res = self.run_step(self.engine, spec, workspace)
        usage = {w.label: w.used_pct for w in reading.windows} if reading else None
        seconds = round(time.monotonic() - t0, 1)
        record = StepRecord(role, model, effort, res.ok, seconds, res.tool_uses, res.cost_usd, res.error, usage)
        self.report.steps.append(record)
        if self.on_step:
            self.on_step(record)
        return res


def table(steps: Sequence[StepRecord]) -> list[str]:
    """The steps of a report as a Markdown table: model, outcome, time, tools, cost and the quota before each one."""
    rows = ["| step | model | ok | seconds | tools | cost (USD eq.) | quota before |", "|---" * 7 + "|"]
    for s in steps:
        quota = ", ".join(f"{k} {v:g}%" for k, v in (s.quota_before or {}).items()) or "-"
        cost = f"{s.cost_usd:.2f}" if s.cost_usd is not None else "-"
        rows.append(
            f"| {s.role} | {s.model} {s.effort or ''} | {'yes' if s.ok else 'no'} | {s.seconds:g} | "
            f"{s.tool_uses} | {cost} | {quota} |"
        )
    return rows
