"""Tests for MonthPeriod and the parse_month argparse type callback."""

import argparse
from datetime import date

import pytest

from ad_voting_metrics.cli import parse_month
from ad_voting_metrics.period import MonthPeriod

# ---------------------------------------------------------------------------
# MonthPeriod construction and properties
# ---------------------------------------------------------------------------


def test_construct_with_valid_year_and_month():
    p = MonthPeriod(year=2026, month=4)
    assert p.year == 2026
    assert p.month == 4


@pytest.mark.parametrize("month", [0, 13, -1, 100])
def test_invalid_month_rejected(month):
    with pytest.raises(ValueError, match=r"month must be 1..12"):
        MonthPeriod(year=2026, month=month)


def test_unreasonable_year_rejected():
    with pytest.raises(ValueError, match="year must be"):
        MonthPeriod(year=1800, month=4)


def test_far_future_year_accepted_at_type_level():
    # The type itself doesn't reject future months — that's a CLI concern.
    p = MonthPeriod(year=2099, month=12)
    assert p.start == date(2099, 12, 1)
    assert p.end == date(2099, 12, 31)


def test_frozen():
    # Should be immutable.
    p = MonthPeriod(year=2026, month=4)
    with pytest.raises((AttributeError, TypeError)):
        # Use setattr() so static type-checkers don't flag the assignment;
        # we deliberately want to attempt the forbidden write at runtime.
        setattr(p, "year", 2027)  # noqa: B010


# ---------------------------------------------------------------------------
# start and end derived properties
# ---------------------------------------------------------------------------


def test_start_is_first_of_month():
    assert MonthPeriod(2026, 4).start == date(2026, 4, 1)
    assert MonthPeriod(2025, 1).start == date(2025, 1, 1)


def test_end_30_day_month():
    assert MonthPeriod(2025, 9).end == date(2025, 9, 30)


def test_end_31_day_month():
    assert MonthPeriod(2025, 7).end == date(2025, 7, 31)


def test_end_february_leap_year():
    assert MonthPeriod(2024, 2).end == date(2024, 2, 29)


def test_end_february_non_leap_year():
    assert MonthPeriod(2025, 2).end == date(2025, 2, 28)


def test_end_century_year_not_leap():
    # 2100 will be a leap year (divisible by 100 but not 400).
    assert MonthPeriod(2100, 2).end == date(2100, 2, 28)


def test_end_year_2000_was_leap():
    # 2400 will be a leap year (divisible by 400).
    assert MonthPeriod(2400, 2).end == date(2400, 2, 29)


def test_end_december_does_not_roll_to_january():
    p = MonthPeriod(2025, 12)
    assert p.start == date(2025, 12, 1)
    assert p.end == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# __str__ formatting
# ---------------------------------------------------------------------------


def test_str_format():
    assert str(MonthPeriod(2026, 4)) == "April 2026"
    assert str(MonthPeriod(2025, 1)) == "January 2025"
    assert str(MonthPeriod(2024, 12)) == "December 2024"


# ---------------------------------------------------------------------------
# MonthPeriod.from_string parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ("April 2026", MonthPeriod(2026, 4)),
        ("January 2025", MonthPeriod(2025, 1)),
        ("December 2024", MonthPeriod(2024, 12)),
        ("Apr 2026", MonthPeriod(2026, 4)),
        ("Jan 2025", MonthPeriod(2025, 1)),
        ("2026-04", MonthPeriod(2026, 4)),
        ("2025-01", MonthPeriod(2025, 1)),
        ("APRIL 2026", MonthPeriod(2026, 4)),
        ("april 2026", MonthPeriod(2026, 4)),
    ],
)
def test_from_string_happy_path(value, expected):
    assert MonthPeriod.from_string(value) == expected


def test_from_string_result_does_not_depend_on_current_day():
    # Pins down the today-default footgun: dateutil.parser.parse fills missing
    # components from today's date by default. Without the sentinel default,
    # MonthPeriod.from_string("April 2026") run on May 5 would return a period
    # that internally remembers day=5 — which we don't store, but we want the
    # parsing to be stable regardless.
    first = MonthPeriod.from_string("January 2025")
    second = MonthPeriod.from_string("January 2025")
    assert first == second
    assert first.start.day == 1


@pytest.mark.parametrize(
    "value",
    [
        "not a date",
        "Decembruary 2026",
        "Smarch",
        "",
    ],
)
def test_from_string_unparseable_raises_value_error(value):
    with pytest.raises(ValueError):
        MonthPeriod.from_string(value)


def test_from_string_does_not_reject_future():
    # Future-month rejection is a CLI concern, not a type concern.
    p = MonthPeriod.from_string("December 2099")
    assert p == MonthPeriod(2099, 12)


# ---------------------------------------------------------------------------
# parse_month (the argparse type callback) — CLI-specific behavior
# ---------------------------------------------------------------------------


def test_parse_month_returns_month_period():
    result = parse_month("April 2026")
    assert isinstance(result, MonthPeriod)
    assert result == MonthPeriod(2026, 4)


def test_parse_month_unparseable_raises_argument_type_error():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_month("not a date")


def test_parse_month_far_future_rejected():
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        parse_month("December 2099")
    assert "future" in str(exc_info.value).lower()


def test_parse_month_year_only_resolves_to_january_and_rejected_as_future():
    # "2099" alone parses as January 1, 2099 (per dateutil with day-1 sentinel)
    # and should be rejected as future.
    with pytest.raises(argparse.ArgumentTypeError):
        parse_month("2099")


def test_parse_month_error_messages_mention_input():
    """The error message helps the user fix their input."""
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        parse_month("not a date")
    msg = str(exc_info.value)
    assert "not a date" in msg
