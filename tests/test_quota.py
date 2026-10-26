from datetime import UTC, datetime

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
    ledger_reading,
    month,
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


def test_the_burst_rate_uses_the_accounts_own_windows() -> None:
    from lastcall.quota import account_rate

    def day(used: float, five: float, t: float) -> Reading:
        windows = (Window("day", used, 1e9, "expiring", 24.0), Window("1h", five, 5e8, "throttle", 1.0))
        return Reading("x", windows, False, "exact", t)

    readings = [day(10, 0, 0), day(30, 40, 1), day(50, 80, 2)]
    # 40 points of the day per 80 points of the hourly window: 0.5 each, and 90% of it per hour
    assert account_rate(readings, readings[-1], SpendPolicy(), 2.0) == pytest.approx(0.5 * 90 / 1.0)
    alone = Reading("x", (Window("week", 10, 1e9, "expiring"),), False, "exact", 3)
    assert account_rate([alone], alone, SpendPolicy(), 2.0) == 2.0  # no throttle window: the default


# windows of one model


def with_opus(opus: float) -> Reading:
    base = reading(week=30, week_reset_h=33)
    model = Window("week", opus, NOW + 50 * HOUR, "expiring", 168.0, "Opus")
    return Reading(base.account, (*base.windows, model), False, "exact", NOW)


def test_a_models_window_limits_only_the_steps_on_that_model() -> None:
    r = with_opus(95)
    assert gate(r, POLICY, NOW).ok  # without models, only the account's windows count
    assert gate(r, POLICY, NOW, ["sonnet", "haiku"]).ok
    closed = gate(r, POLICY, NOW, ["sonnet", "claude-opus-5-5"])
    assert not closed.ok and closed.reason == "week Opus window at 95% (reserve 10%)"
    assert closed.wait_seconds == 0  # a weekly window stops the run; it does not make it wait


def test_model_names_match_word_by_word() -> None:
    opus = Window("week", 50, None, "expiring", model="Opus")
    assert opus.limits("opus") and opus.limits("opus[1m]") and opus.limits("claude-opus-5-5")
    assert not opus.limits("haiku") and not opus.limits("opusplan") and not opus.limits("")
    assert Window("week", 50, None, "expiring", model="Fable").limits("claude-fable-5-1")
    assert Window("week", 50, None, "expiring").limits("anything")  # the account's window limits every step


def test_a_models_window_is_not_the_accounts() -> None:
    r = with_opus(80)
    assert r.window("week") == r.windows[0]
    assert [w.label for w in r.expiring] == ["week"]
    assert [w.label for w in r.limiting(["opus"])] == ["week", "5h", "week Opus"]
    assert [w.label for w in r.limiting(["haiku"])] == ["week", "5h"]


def test_no_burst_while_a_model_it_needs_is_out_of_room() -> None:
    p = plan(with_opus(92), POLICY, 2.0, NOW, ["sonnet", "opus"])
    assert not p.burst and p.code == "model"
    assert p.full is not None and p.full.model == "Opus"
    q = plan(with_opus(92), POLICY, 2.0, NOW, ["sonnet"])  # a burst that does not need Opus
    assert q.burst and q.leftover_pct == 60  # the model's window does not change the leftover


# accounts on API keys and local models: the ledger, the budget and the schedule


def at(year: int, month: int, day: int, hour: int = 0) -> float:
    return datetime(year, month, day, hour, tzinfo=UTC).timestamp()


def test_the_ledger_reads_this_months_spending_against_the_budget() -> None:
    now = at(2026, 10, 11)
    r = ledger_reading("openrouter", 20.0, 5.0, now)
    (w,) = r.windows
    assert (w.name, w.role, w.used_pct, r.fidelity) == ("budget", "expiring", 25.0, "ledger")
    assert w.resets_at == at(2026, 11, 1)
    assert w.duration_hours == 31 * 24
    assert month(at(2026, 12, 31, 23)) == (at(2026, 12, 1), at(2027, 1, 1))
    assert ledger_reading("local", None, 0.0, now).windows == ()  # a local model has nothing to count against


def test_a_budget_is_spent_at_the_pace_of_the_month() -> None:
    policy = SpendPolicy(kind="budget", reserve_pct=0)
    now = at(2026, 10, 11)  # 10 of October's 31 days are gone
    behind = plan(ledger_reading("key", 20.0, 5.0, now), policy, 2.0, now)
    assert behind.burst and behind.code == "pace"
    ahead = plan(ledger_reading("key", 20.0, 8.0, now), policy, 2.0, now)
    assert not ahead.burst and ahead.code == "ahead"
    assert ahead.starts_in_hours == pytest.approx(0.40 * 31 * 24 - 10 * 24)  # when 40% of the month is gone
    spent = plan(ledger_reading("key", 20.0, 19.9, now), policy, 2.0, now)
    assert not spent.burst and spent.code == "spent" and spent.budget is not None
    assert plan(ledger_reading("key", None, 0.0, now), policy, 2.0, now).code == "no-budget"


def test_a_schedule_bursts_at_its_hours_while_the_budget_has_room() -> None:
    nights = SpendPolicy(kind="schedule", reserve_pct=0, cron="* 1-6 * * *")
    night, noon = at(2026, 10, 26, 2), at(2026, 10, 26, 12)
    assert plan(ledger_reading("local", None, 0.0, night), nights, 2.0, night).code == "schedule"
    later = plan(ledger_reading("local", None, 0.0, noon), nights, 2.0, noon)
    assert not later.burst and later.code == "window"
    assert later.starts_in_hours == pytest.approx(13.0)
    assert plan(ledger_reading("key", 20.0, 20.0, night), nights, 2.0, night).code == "spent"
    wrong = SpendPolicy(kind="schedule", cron="every night")
    assert plan(ledger_reading("local", None, 0.0, noon), wrong, 2.0, noon).code == "bad-schedule"
    never = SpendPolicy(kind="schedule", cron="0 0 31 2 *")
    assert plan(ledger_reading("local", None, 0.0, noon), never, 2.0, noon).code == "never"


def test_the_gate_stops_an_account_on_a_key_at_its_budget() -> None:
    now = at(2026, 10, 11)
    policy = SpendPolicy(kind="budget", reserve_pct=0)
    assert gate(ledger_reading("key", 20.0, 19.0, now), policy, now).ok
    closed = gate(ledger_reading("key", 20.0, 20.5, now), policy, now)
    assert not closed.ok and closed.reason == "this month's budget is spent (102%)"
    always = SpendPolicy(kind="schedule", reserve_pct=0, cron="* * * * *")
    assert gate(ledger_reading("local", None, 0.0, now), always, now).ok


# a reading whose reset has passed: its leftover is gone


def test_no_burst_once_the_reset_has_passed() -> None:
    # read before the reset, planned after it: the leftover of that window is gone, and the next reading tells
    old = reading(week=68, week_reset_h=-25)
    p = plan(old, POLICY, 2.0, NOW)
    assert not p.burst and p.code == "passed" and p.starts_in_hours is None
    assert plan(reading(week=68, week_reset_h=0), POLICY, 2.0, NOW).code == "passed"  # the very second it resets
    assert plan(reading(week=97, week_reset_h=-1), POLICY, 2.0, NOW).code == "passed"  # however much it had left
    assert plan(reading(week=68, week_reset_h=1), POLICY, 2.0, NOW).code == "now"  # a reset still ahead bursts
    assert plan(old, SpendPolicy(kind="manual"), 2.0, NOW).code == "manual"  # the policy says it first


def test_a_budget_or_a_schedule_from_a_past_month_never_bursts() -> None:
    november = at(2026, 11, 2)
    october = ledger_reading("key", 20.0, 1.0, at(2026, 10, 11))  # behind the pace of October, read in October
    p = plan(october, SpendPolicy(kind="budget", reserve_pct=0), 2.0, november)
    assert not p.burst and p.code == "passed"
    nights = SpendPolicy(kind="schedule", reserve_pct=0, cron="* * * * *")
    assert plan(october, nights, 2.0, november).code == "passed"
    spent = ledger_reading("key", 20.0, 20.0, at(2026, 10, 11))
    assert plan(spent, nights, 2.0, november).code == "passed"  # October's spending says nothing of November
    assert plan(ledger_reading("local", None, 0.0, november), nights, 2.0, november).code == "schedule"
