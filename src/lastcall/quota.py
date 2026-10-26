"""Quota readings, spend policies and the gates that run before every step.

A reading is a list of windows. Expiring windows decide whether to spend: their leftover is lost at the reset (the
weekly window of a subscription). Throttle windows decide when to wait: when one is nearly full, the next step waits
for its reset (the 5-hour window). A window can also limit one model only (Claude's weekly window for Opus): it does
not decide whether to spend, but the steps on that model stop when it reaches the reserve. See DESIGN.md, section 6.

An account on an API key or a local model has no window of its provider. Its reading comes from lastcall's ledger of
what its steps cost: one expiring window, its monthly budget. Its policy is `budget` (spend the budget at the pace of
the calendar month), `schedule` (burst at the hours of a cron expression, in UTC) or `manual`.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from lastcall import cron

Role = Literal["expiring", "throttle"]
Fidelity = Literal["exact", "events", "ledger", "none"]
Kind = Literal["before-reset", "budget", "schedule", "manual"]

HOUR = 3600.0
KINDS: tuple[Kind, ...] = ("before-reset", "budget", "schedule", "manual")
BUDGET = "budget"  # the window of an account's monthly budget, in a reading from the ledger


class QuotaError(RuntimeError):
    """The quota could not be read. Without a reading nothing is spent."""


def epoch(value: object) -> float | None:
    """Epoch seconds from what providers send: an ISO 8601 string or a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        return datetime.fromisoformat(value).timestamp()
    raise QuotaError(f"not a time: {value!r}")


@dataclass(frozen=True)
class Window:
    name: str  # "5h", "week", "day", "month"
    used_pct: float
    resets_at: float | None  # epoch seconds; None when the provider does not say
    role: Role
    duration_hours: float | None = None
    model: str = ""  # the model it limits, as the provider names it ("Opus"); "" when it limits the whole account

    @property
    def label(self) -> str:
        """The name, with the model when the window limits only one: "week", "week Opus"."""
        return f"{self.name} {self.model}" if self.model else self.name

    def limits(self, model: str) -> bool:
        """Whether the window limits a step on `model`. An account's window limits every step. A model's window
        limits the steps whose model name holds the window's, word by word: "Opus" limits "opus", "opus[1m]" and
        "claude-opus-5-5", and not "haiku"."""
        return not self.model or _words(self.model) in _words(model)


def _words(name: str) -> str:
    return "-" + "-".join(re.findall(r"[a-z0-9]+", name.lower())) + "-"


@dataclass(frozen=True)
class Reading:
    account: str
    windows: tuple[Window, ...]
    paid_usage: bool  # extra usage, credits or overage billing active on the account: never spend
    fidelity: Fidelity
    taken_at: float

    def window(self, name: str) -> Window | None:
        """The account's window of that name; never a model's."""
        return next((w for w in self.windows if w.name == name and not w.model), None)

    @property
    def expiring(self) -> tuple[Window, ...]:
        """The account's expiring windows, which decide whether to spend."""
        return tuple(w for w in self.windows if w.role == "expiring" and not w.model)

    @property
    def throttles(self) -> tuple[Window, ...]:
        """The account's throttle windows."""
        return tuple(w for w in self.windows if w.role == "throttle" and not w.model)

    def limiting(self, models: Sequence[str]) -> tuple[Window, ...]:
        """The windows that limit steps on these models: the account's, and those of each model."""
        return tuple(w for w in self.windows if not w.model or any(w.limits(m) for m in models))


@dataclass(frozen=True)
class SpendPolicy:
    kind: Kind = "before-reset"
    reserve_pct: float = 10.0  # share of every expiring window that stays for the owner
    throttle_max_pct: float = 90.0  # above this, a step waits for the throttle window to reset
    max_lead_hours: float = 48.0  # a burst never starts earlier than this before the reset
    min_leftover_pct: float = 2.0  # a smaller leftover is not worth a burst
    cron: str = ""  # kind schedule: when it bursts, in UTC (lastcall.cron)


@dataclass(frozen=True)
class Plan:
    burst: bool
    reason: str
    leftover_pct: float = 0.0
    hours_to_reset: float | None = None
    hours_needed: float = 0.0
    starts_in_hours: float | None = None
    code: str = ""  # the reason, for the web UI: manual, paid, no-window, model, no-reset, small, now, later...
    full: Window | None = None  # code "model": the window of a model that the burst needs, at the reserve
    budget: Window | None = None  # the monthly budget of an account on an API key


def plan(
    reading: Reading, policy: SpendPolicy, rate_pct_per_hour: float, now: float, models: Sequence[str] = ()
) -> Plan:
    """Whether the account should burst now.

    The burst starts when the time left until the reset is just enough to spend the leftover at the rate a burst
    can sustain (with a 10% margin and one extra hour), and never more than `max_lead_hours` before the reset. With
    several expiring windows, the one that resets first sets the deadline and the smallest leftover sets the amount.
    `models` are those the burst's steps use: while the window of one of them is at the reserve, those steps could
    not run, so there is no burst.
    """
    if policy.kind == "manual":
        return Plan(False, "policy manual: only runs started by the owner", code="manual")
    if reading.paid_usage:
        return Plan(False, "paid usage is active on this account", code="paid")
    if policy.kind == "budget":
        return _budget(reading, policy, now)
    if policy.kind == "schedule":
        return _schedule(reading, policy, now)
    windows = reading.expiring
    if not windows:
        return Plan(False, "no expiring window to spend", code="no-window")
    full = at_reserve((w for w in reading.limiting(models) if w.model), policy)
    if full:
        return Plan(
            False,
            f"{full.label} window at {full.used_pct:g}% (reserve {policy.reserve_pct:g}%)",
            code="model",
            full=full,
        )
    leftover = min(100.0 - policy.reserve_pct - w.used_pct for w in windows)
    resets = [w.resets_at for w in windows if w.resets_at is not None]
    if not resets:
        return Plan(False, "reset time unknown", leftover_pct=leftover, code="no-reset")
    hours_to_reset = (min(resets) - now) / HOUR
    hours_needed = max(0.0, leftover) / max(rate_pct_per_hour, 0.1)
    starts_in = hours_to_reset - min(policy.max_lead_hours, hours_needed * 1.1 + 1)
    if leftover <= policy.min_leftover_pct:
        reason = f"leftover {leftover:.1f}% is not worth a burst"
        return Plan(False, reason, leftover, hours_to_reset, hours_needed, code="small")
    burst = starts_in <= 0
    reason = f"{leftover:.1f}% left, {hours_to_reset:.1f} h to the reset" if burst else f"burst in {starts_in:.1f} h"
    code = "now" if burst else "later"
    return Plan(burst, reason, leftover, hours_to_reset, hours_needed, max(0.0, starts_in), code)


def _budget(reading: Reading, policy: SpendPolicy, now: float) -> Plan:
    """Spend a monthly budget at the pace of the month: burst while the share spent is behind the share of the month
    gone, so that a burst early in the month leaves room for the rest of it."""
    w = reading.window(BUDGET)
    if w is None or w.resets_at is None or not w.duration_hours:
        return Plan(False, "no monthly budget", code="no-budget")
    hours_to_reset = (w.resets_at - now) / HOUR
    leftover = 100.0 - policy.reserve_pct - w.used_pct
    if leftover <= policy.min_leftover_pct:
        reason = f"budget spent ({w.used_pct:.0f}%)"
        return Plan(False, reason, max(0.0, leftover), hours_to_reset, code="spent", budget=w)
    gone = 100.0 * (1.0 - hours_to_reset / w.duration_hours)
    if w.used_pct < gone:
        reason = f"{w.used_pct:.0f}% of the budget spent and {gone:.0f}% of the month gone"
        return Plan(True, reason, leftover, hours_to_reset, code="pace", budget=w)
    starts_in = (w.used_pct - gone) / 100.0 * w.duration_hours
    reason = f"burst in {starts_in:.1f} h, when the month catches up with the spending"
    return Plan(False, reason, leftover, hours_to_reset, starts_in_hours=starts_in, code="ahead", budget=w)


def _schedule(reading: Reading, policy: SpendPolicy, now: float) -> Plan:
    """Burst in the minutes of a cron expression, while the monthly budget, when the account has one, has room."""
    w = reading.window(BUDGET)
    if w is not None and w.used_pct >= 100.0 - policy.reserve_pct:
        hours = (w.resets_at - now) / HOUR if w.resets_at is not None else None
        return Plan(False, f"budget spent ({w.used_pct:.0f}%)", 0.0, hours, code="spent", budget=w)
    try:
        when = cron.parse(policy.cron)
    except ValueError as e:
        return Plan(False, str(e), code="bad-schedule")
    if when.matches(now):
        return Plan(True, f"inside the schedule {when.text} (UTC)", code="schedule", budget=w)
    start = when.next(now)
    if start is None:
        return Plan(False, f"the schedule {when.text} never comes", code="never")
    hours = (start - now) / HOUR
    return Plan(False, f"burst in {hours:.1f} h, by the schedule", starts_in_hours=hours, code="window", budget=w)


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: str = ""
    wait_seconds: float = 0.0  # > 0: wait this long for a throttle window to reset, then check again


def gate(reading: Reading | None, policy: SpendPolicy, now: float, models: Sequence[str] = ()) -> Gate:
    """The checks before every step, against the account's windows and those of the step's models. Without a
    reading nothing is spent."""
    if reading is None:
        return Gate(False, "no quota reading")
    if reading.paid_usage:
        return Gate(False, "paid usage is active on this account")
    windows = reading.limiting(models)
    full = at_reserve(windows, policy)
    if full and reading.fidelity == "ledger" and full.name == BUDGET:
        return Gate(False, f"this month's budget is spent ({full.used_pct:.0f}%)")
    if full:
        return Gate(False, f"{full.label} window at {full.used_pct:g}% (reserve {policy.reserve_pct:g}%)")
    for w in windows:
        if w.role == "throttle" and w.used_pct >= policy.throttle_max_pct:
            reset = w.resets_at if w.resets_at is not None else now + 600.0
            return Gate(False, f"{w.label} window at {w.used_pct:g}%", max(60.0, reset - now + 60.0))
    return Gate(True)


def at_reserve(windows: Iterable[Window], policy: SpendPolicy) -> Window | None:
    """The first expiring window whose use has reached the owner's reserve."""
    return next((w for w in windows if w.role == "expiring" and w.used_pct >= 100.0 - policy.reserve_pct), None)


def ledger_reading(account: str, budget_usd: float | None, spent_usd: float, now: float) -> Reading:
    """The reading of an account on an API key or a local model, from lastcall's ledger: what its steps cost in this
    calendar month (UTC), as a share of its monthly budget. Without a budget it has no window."""
    if not budget_usd:
        return Reading(account, (), False, "none", now)
    start, end = month(now)
    window = Window(BUDGET, 100.0 * spent_usd / budget_usd, end, "expiring", (end - start) / HOUR)
    return Reading(account, (window,), False, "ledger", now)


def month(now: float) -> tuple[float, float]:
    """The calendar month (UTC) of `now`: when it started and when it ends, in epoch seconds."""
    start = datetime.fromtimestamp(now, UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def learn_ratio(
    readings: Sequence[Reading], expiring: str, throttle: str, min_throttle_pct: float = 30.0
) -> float | None:
    """Points of the expiring window that one point of the throttle window costs, learned from past readings.

    Uses consecutive readings inside the same throttle window and the same expiring window (same reset times) in
    which the throttle window grew. It is a ratio of sums because providers round usage to whole points and single
    pairs are noisy. None until the throttle window has grown by `min_throttle_pct` points in total.
    """
    d_expiring = d_throttle = 0.0
    ordered = sorted(readings, key=lambda r: r.taken_at)
    for a, b in itertools.pairwise(ordered):
        ea, eb, ta, tb = a.window(expiring), b.window(expiring), a.window(throttle), b.window(throttle)
        if ea is None or eb is None or ta is None or tb is None:
            continue
        if not (_same_reset(ea, eb) and _same_reset(ta, tb)):
            continue
        if tb.used_pct > ta.used_pct and eb.used_pct >= ea.used_pct:
            d_throttle += tb.used_pct - ta.used_pct
            d_expiring += eb.used_pct - ea.used_pct
    return d_expiring / d_throttle if d_throttle >= min_throttle_pct else None


def _same_reset(a: Window, b: Window, tolerance: float = 120.0) -> bool:
    return a.resets_at is not None and b.resets_at is not None and abs(a.resets_at - b.resets_at) <= tolerance


def burst_rate(ratio: float, throttle: Window, policy: SpendPolicy) -> float | None:
    """Points of the expiring window per hour that a burst can spend when the throttle window is the bottleneck."""
    if not throttle.duration_hours or throttle.duration_hours <= 0:
        return None
    return ratio * policy.throttle_max_pct / throttle.duration_hours


def account_rate(readings: Sequence[Reading], latest: Reading, policy: SpendPolicy, default: float) -> float:
    """The burst rate learned from an account's readings, between its own windows: the first that expires and the
    first that throttles. `default` when it has no such pair or not enough readings yet."""
    if not latest.expiring or not latest.throttles:
        return default
    return learned_rate(readings, policy, latest.expiring[0].name, latest.throttles[0].name, default)


def learned_rate(
    readings: Sequence[Reading], policy: SpendPolicy, expiring: str, throttle: str, default: float
) -> float:
    """The burst rate learned from the account's readings, or `default` while there is not enough data."""
    if not readings:
        return default
    ratio = learn_ratio(readings, expiring, throttle)
    latest = max(readings, key=lambda r: r.taken_at).window(throttle)
    if ratio is None or latest is None:
        return default
    rate = burst_rate(ratio, latest, policy)
    return rate if rate else default
