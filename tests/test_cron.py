from datetime import UTC, datetime
from pathlib import Path

import pytest

from lastcall import cli
from lastcall.cron import normalize, parse
from lastcall.store import Store


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


PASTED = "*\u00a01\u20116\u00a0*\u00a0*\u00a0*"  # how the plan sentences write "* 1-6 * * *": it stays on one line


def test_a_schedule_copied_from_a_page_is_plain_text_again() -> None:
    assert normalize(PASTED) == "* 1-6 * * *"
    assert normalize("  *\u2009 1\u20106  *\n*\u3000*\u2212 ") == "* 1-6 * * *-"  # other spaces and dashes too
    pasted = parse(PASTED)
    assert pasted.text == "* 1-6 * * *" and pasted.hours == parse("* 1-6 * * *").hours
    assert parse("*/15\u00a022\u201123,0\u20115\u00a0*\u00a0*\u00a01\u20115").text == "*/15 22-23,0-5 * * 1-5"


def test_the_cli_stores_a_pasted_schedule_as_plain_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LASTCALL_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("LASTCALL_SECRETS", str(tmp_path / "secrets"))
    argv = [
        "account",
        "add",
        "box",
        "--engine",
        "opencode",
        "--provider",
        "ollama",
        "--model",
        "qwen3-coder",
        "--policy",
        "schedule",
    ]
    assert cli.main([*argv, "--schedule", PASTED]) == 0
    row = Store(tmp_path / "state" / "lastcall.db").account("box")
    assert row is not None and row.schedule == "* 1-6 * * *"


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
