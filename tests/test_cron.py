from datetime import UTC, datetime

import pytest

from lastcall.cron import parse


def at(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> float:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp()


def test_a_nightly_window() -> None:
    nights = parse("* 1-6 * * *")
    assert nights.matches(at(2026, 10, 26, 1, 0))
    assert nights.matches(at(2026, 10, 26, 6, 59, 59))
    assert not nights.matches(at(2026, 10, 26, 7, 0))
    assert nights.next(at(2026, 10, 26, 7, 0)) == at(2026, 10, 27, 1, 0)
    assert nights.next(at(2026, 10, 26, 0, 59, 30)) == at(2026, 10, 26, 1, 0)


def test_lists_steps_and_weekdays() -> None:
    working_nights = parse("*/15 22-23,0-5 * * 1-5")
    assert working_nights.matches(at(2026, 10, 26, 22, 45))  # a Monday
    assert not working_nights.matches(at(2026, 10, 26, 22, 46))
    saturday = at(2026, 10, 31, 2, 0)
    assert not working_nights.matches(saturday)
    assert working_nights.next(saturday) == at(2026, 11, 2, 0, 0)  # Monday's first quarter
    assert parse("0 3 * * 7").matches(at(2026, 11, 1, 3, 0))  # 7 is Sunday, like 0


def test_a_restricted_day_and_weekday_match_either() -> None:
    first_or_monday = parse("0 0 1 * 1")
    assert first_or_monday.matches(at(2026, 10, 1, 0, 0))  # a Thursday, the 1st
    assert first_or_monday.matches(at(2026, 10, 5, 0, 0))  # a Monday
    assert not first_or_monday.matches(at(2026, 10, 6, 0, 0))


def test_a_rare_day_and_a_day_that_never_comes() -> None:
    assert parse("0 0 29 2 *").next(at(2026, 10, 26)) == at(2028, 2, 29)
    assert parse("0 0 31 2 *").next(at(2026, 10, 26)) is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("* * *", "five fields"),
        ("60 * * * *", "minute of a schedule goes from 0 to 59"),
        ("* 1-24 * * *", "hour of a schedule goes from 0 to 23"),
        ("5-1 * * * *", "goes from 0 to 59"),
        ("*/0 * * * *", "steps of a schedule are 1 or more"),
        ("a * * * *", "takes numbers"),
        ("* * * * mon", "takes numbers"),
    ],
)
def test_mistakes_say_what_is_wrong(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(text)
