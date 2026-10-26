"""Cron expressions for the `schedule` spend policy: five fields (minute, hour, day of the month, month, weekday), in
UTC. An account on that policy bursts during the minutes that match: `* 1-6 * * *` is every night from 01:00 to
06:59 UTC, and `* 1-6 * * 1-5` the same hours from Monday to Friday.

Each field takes `*`, a number, a range `a-b`, a step `*/n` or `a-b/n`, and lists of them separated by commas.
Weekdays go from 0 (Sunday) to 6, and 7 is Sunday too. As in cron, when both the day of the month and the weekday
are restricted, a day matches either of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of the month", 1, 31), ("month", 1, 12), ("weekday", 0, 7))
SEARCH_DAYS = 4 * 366 + 1  # enough to find any day that exists, 29 February included


@dataclass(frozen=True)
class Cron:
    text: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 is Sunday
    every_day: bool  # the day of the month starts with `*`
    every_weekday: bool  # the day of the week starts with `*`

    def matches(self, when: float) -> bool:
        t = datetime.fromtimestamp(when, UTC)
        return t.minute in self.minutes and t.hour in self.hours and self._day(t)

    def next(self, after: float) -> float | None:
        """The start of the first matching minute after `after`; None when no day ever matches."""
        t = datetime.fromtimestamp(after, UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = t.replace(hour=0, minute=0)
        times = sorted((h, m) for h in self.hours for m in self.minutes)
        for _ in range(SEARCH_DAYS):
            if self._day(day):
                for h, m in times:
                    candidate = day.replace(hour=h, minute=m)
                    if candidate >= t:
                        return candidate.timestamp()
            day += timedelta(days=1)
        return None

    def _day(self, t: datetime) -> bool:
        if t.month not in self.months:
            return False
        day, weekday = t.day in self.days, t.isoweekday() % 7 in self.weekdays
        if self.every_day or self.every_weekday:
            return day and weekday
        return day or weekday


def parse(text: str) -> Cron:
    """A cron expression, or ValueError that says what is wrong with it."""
    parts = text.split()
    if len(parts) != 5:
        raise ValueError(
            f"a schedule has five fields (minute, hour, day of the month, month, weekday), such as `* 1-6 * * *`: "
            f"{text!r}"
        )
    minutes, hours, days, months, weekdays = (_field(part, *spec) for part, spec in zip(parts, FIELDS, strict=True))
    sunday = frozenset(d % 7 for d in weekdays)  # 7 is Sunday too
    return Cron(
        " ".join(parts), minutes, hours, days, months, sunday, parts[2].startswith("*"), parts[4].startswith("*")
    )


def _field(text: str, name: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for item in text.split(","):
        base, slash, step_text = item.partition("/")
        try:
            step = int(step_text) if slash else 1
            if base == "*":
                first, last = low, high
            elif "-" in base:
                start, end = base.split("-", 1)
                first, last = int(start), int(end)
            else:
                first = int(base)
                last = high if slash else first  # `5/15`: from 5, every 15
        except ValueError:
            raise ValueError(
                f"the {name} of a schedule takes numbers, ranges `a-b`, steps `/n` and `*`: {text!r}"
            ) from None
        if step < 1:
            raise ValueError(f"the steps of a schedule are 1 or more: {text!r}")
        if not low <= first <= last <= high:
            raise ValueError(f"the {name} of a schedule goes from {low} to {high}: {text!r}")
        values.update(range(first, last + 1, step))
    return frozenset(values)
