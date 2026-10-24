"""Quota readings, spend policies and the gates that run before every step.

A reading is a list of windows. Expiring windows decide whether to spend: their leftover is lost at the reset (the
weekly window of a subscription). Throttle windows decide when to wait: when one is nearly full, the next step waits
for its reset (the 5-hour window). See DESIGN.md, section 6.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Role = Literal["expiring", "throttle"]
Fidelity = Literal["exact", "events", "ledger", "none"]

HOUR = 3600.0


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


@dataclass(frozen=True)
class Reading:
    account: str
    windows: tuple[Window, ...]
    paid_usage: bool  # extra usage, credits or overage billing active on the account: never spend
    fidelity: Fidelity
    taken_at: float

    def window(self, name: str) -> Window | None:
        return next((w for w in self.windows if w.name == name), None)

    @property
    def expiring(self) -> tuple[Window, ...]:
        return tuple(w for w in self.windows if w.role == "expiring")

    @property
    def throttles(self) -> tuple[Window, ...]:
        return tuple(w for w in self.windows if w.role == "throttle")


@dataclass(frozen=True)
class SpendPolicy:
    kind: Literal["before-reset", "manual"] = "before-reset"
    reserve_pct: float = 10.0  # share of every expiring window that stays for the owner
    throttle_max_pct: float = 90.0  # above this, a step waits for the throttle window to reset
    max_lead_hours: float = 48.0  # a burst never starts earlier than this before the reset
    min_leftover_pct: float = 2.0  # a smaller leftover is not worth a burst


@dataclass(frozen=True)
class Plan:
    burst: bool
    reason: str
    leftover_pct: float = 0.0
    hours_to_reset: float | None = None
    hours_needed: float = 0.0
    starts_in_hours: float | None = None


def plan(reading: Reading, policy: SpendPolicy, rate_pct_per_hour: float, now: float) -> Plan:
    """Whether the account should burst now.

    The burst starts when the time left until the reset is just enough to spend the leftover at the rate a burst
    can sustain (with a 10% margin and one extra hour), and never more than `max_lead_hours` before the reset. With
    several expiring windows, the one that resets first sets the deadline and the smallest leftover sets the amount.
    """
    if policy.kind != "before-reset":
        return Plan(False, f"policy {policy.kind}: only runs started by the owner")
    if reading.paid_usage:
        return Plan(False, "paid usage is active on this account")
    windows = reading.expiring
    if not windows:
        return Plan(False, "no expiring window to spend")
    leftover = min(100.0 - policy.reserve_pct - w.used_pct for w in windows)
    resets = [w.resets_at for w in windows if w.resets_at is not None]
    if not resets:
        return Plan(False, "reset time unknown", leftover_pct=leftover)
    hours_to_reset = (min(resets) - now) / HOUR
    hours_needed = max(0.0, leftover) / max(rate_pct_per_hour, 0.1)
    starts_in = hours_to_reset - min(policy.max_lead_hours, hours_needed * 1.1 + 1)
    if leftover <= policy.min_leftover_pct:
        return Plan(False, f"leftover {leftover:.1f}% is not worth a burst", leftover, hours_to_reset, hours_needed)
    burst = starts_in <= 0
    reason = f"{leftover:.1f}% left, {hours_to_reset:.1f} h to the reset" if burst else f"burst in {starts_in:.1f} h"
    return Plan(burst, reason, leftover, hours_to_reset, hours_needed, max(0.0, starts_in))


@dataclass(frozen=True)
class Gate:
    ok: bool
    reason: str = ""
    wait_seconds: float = 0.0  # > 0: wait this long for a throttle window to reset, then check again


def gate(reading: Reading | None, policy: SpendPolicy, now: float) -> Gate:
    """The checks before every step. Without a reading nothing is spent."""
    if reading is None:
        return Gate(False, "no quota reading")
    if reading.paid_usage:
        return Gate(False, "paid usage is active on this account")
    for w in reading.expiring:
        if w.used_pct >= 100.0 - policy.reserve_pct:
            return Gate(False, f"{w.name} window at {w.used_pct:g}% (reserve {policy.reserve_pct:g}%)")
    for w in reading.throttles:
        if w.used_pct >= policy.throttle_max_pct:
            reset = w.resets_at if w.resets_at is not None else now + 600.0
            return Gate(False, f"{w.name} window at {w.used_pct:g}%", max(60.0, reset - now + 60.0))
    return Gate(True)


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
