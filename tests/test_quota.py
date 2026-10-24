import pytest

from lastcall.quota import (
    HOUR,
    Reading,
    SpendPolicy,
    Window,
    burst_rate,
    gate,
    learn_ratio,
    learned_rate,
    plan,
)

NOW = 1_800_000_000.0
POLICY = SpendPolicy()


def reading(
    week: float,
    week_reset_h: float,
    five: float = 0.0,
    five_reset_h: float = 2.0,
    paid: bool = False,
    at: float = NOW,
) -> Reading:
    return Reading(
        "claude",
        (
            Window("week", week, NOW + week_reset_h * HOUR, "expiring", 168.0),
            Window("5h", five, NOW + five_reset_h * HOUR, "throttle", 5.0),
        ),
        paid,
        "exact",
        at,
    )


# plan: when to burst


def test_no_burst_while_the_reset_is_far() -> None:
    # 60% left at 2%/h needs 30 h; with the margin, the burst starts 34 h before the reset
    p = plan(reading(week=30, week_reset_h=100), POLICY, 2.0, NOW)
    assert not p.burst
    assert p.leftover_pct == 60
    assert p.starts_in_hours == pytest.approx(66.0)


def test_burst_when_the_time_left_is_just_enough() -> None:
    p = plan(reading(week=30, week_reset_h=33), POLICY, 2.0, NOW)
    assert p.burst
    assert p.starts_in_hours == 0


def test_lead_never_exceeds_max_lead_hours() -> None:
    # 88% left at 1%/h would need 88 h, but a burst never starts more than 48 h before the reset
    p = plan(reading(week=2, week_reset_h=60), POLICY, 1.0, NOW)
    assert not p.burst
    assert p.starts_in_hours == pytest.approx(12.0)


def test_small_leftover_is_not_worth_a_burst() -> None:
    p = plan(reading(week=89, week_reset_h=1), POLICY, 2.0, NOW)
    assert not p.burst
    assert p.leftover_pct == pytest.approx(1.0)


def test_paid_usage_never_bursts() -> None:
    assert not plan(reading(week=10, week_reset_h=1, paid=True), POLICY, 2.0, NOW).burst


def test_manual_policy_never_bursts() -> None:
    assert not plan(reading(week=10, week_reset_h=1), SpendPolicy(kind="manual"), 2.0, NOW).burst


def test_unknown_reset_never_bursts() -> None:
    r = Reading("codex", (Window("week", 10, None, "expiring"),), False, "exact", NOW)
    p = plan(r, POLICY, 2.0, NOW)
    assert not p.burst
    assert p.reason == "reset time unknown"


def test_first_reset_sets_the_deadline_and_smallest_leftover_the_amount() -> None:
    r = Reading(
        "api",
        (
            Window("month", 70, NOW + 300 * HOUR, "expiring"),
            Window("week", 20, NOW + 10 * HOUR, "expiring"),
        ),
        False,
        "ledger",
        NOW,
    )
    p = plan(r, POLICY, 2.0, NOW)
    assert p.leftover_pct == 20  # 100 - 10 reserve - 70 used in the month
    assert p.hours_to_reset == pytest.approx(10.0)
    assert p.burst  # 20% at 2%/h needs 10 h, plus the margin: 12 h


# gate: before every step


def test_gate_lets_a_step_run() -> None:
    assert gate(reading(week=50, week_reset_h=10, five=40), POLICY, NOW).ok


def test_gate_stops_without_reading() -> None:
    g = gate(None, POLICY, NOW)
    assert not g.ok
    assert g.wait_seconds == 0


def test_gate_stops_on_paid_usage() -> None:
    g = gate(reading(week=10, week_reset_h=10, paid=True), POLICY, NOW)
    assert not g.ok
    assert g.wait_seconds == 0


def test_gate_stops_at_the_reserve() -> None:
    g = gate(reading(week=90, week_reset_h=10), POLICY, NOW)
    assert not g.ok
    assert g.wait_seconds == 0


def test_gate_waits_for_a_full_throttle_window() -> None:
    g = gate(reading(week=50, week_reset_h=10, five=95, five_reset_h=2), POLICY, NOW)
    assert not g.ok
    assert g.wait_seconds == pytest.approx(2 * HOUR + 60)


# learned rate


def series(points: list[tuple[float, float, float, float]]) -> list[Reading]:
    """(hours from NOW, week %, 5h %, hours from NOW to the 5h reset), all in the same week."""
    return [
        Reading(
            "claude",
            (
                Window("week", week, NOW + 100 * HOUR, "expiring", 168.0),
                Window("5h", five, NOW + five_reset * HOUR, "throttle", 5.0),
            ),
            False,
            "exact",
            NOW + t * HOUR,
        )
        for t, week, five, five_reset in points
    ]


def test_learn_ratio_inside_one_throttle_window() -> None:
    readings = series([(0, 2, 0, 5), (1, 4, 20, 5), (2, 7, 50, 5)])
    assert learn_ratio(readings, "week", "5h") == pytest.approx(0.1)


def test_learn_ratio_ignores_pairs_across_a_reset() -> None:
    # the third reading is in a new 5-hour window: only the first pair counts, and 20 points are not enough
    readings = series([(0, 2, 0, 5), (1, 4, 20, 5), (6, 5, 10, 10)])
    assert learn_ratio(readings, "week", "5h") is None


def test_learned_rate_uses_the_throttle_window() -> None:
    readings = series([(0, 2, 0, 5), (1, 4, 20, 5), (2, 7, 50, 5)])
    # 0.1 points of the week per point of the 5-hour window, 90 points usable every 5 hours
    assert learned_rate(readings, POLICY, "week", "5h", default=2.0) == pytest.approx(1.8)


def test_learned_rate_falls_back_to_the_default() -> None:
    assert learned_rate([], POLICY, "week", "5h", default=2.0) == 2.0
    assert learned_rate(series([(0, 2, 0, 5)]), POLICY, "week", "5h", default=2.0) == 2.0


def test_burst_rate_needs_the_window_duration() -> None:
    assert burst_rate(0.1, Window("5h", 0, None, "throttle"), POLICY) is None
