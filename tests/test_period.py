"""Tests for main.parse_month - the --month arparse typing callback."""

import argparse
from datetime import date

import pytest

from main import parse_month

# ---------------------------------------------------------------------------
# Happy paths: month strings parse to (first_day, last_day) of the month.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected_start, expected_end",
    [
        # Full month names
        ("April 2026", date(2026, 4, 1), date(2026, 4, 30)),
        ("January 2025", date(2025, 1, 1), date(2025, 1, 31)),
        ("December 2024", date(2024, 12, 1), date(2024, 12, 31)),
        # Abbreviated month names
        ("Apr 2026", date(2026, 4, 1), date(2026, 4, 30)),
        ("Jan 2025", date(2025, 1, 1), date(2025, 1, 31)),
        # ISO-style year-month
        ("2026-04", date(2026, 4, 1), date(2026, 4, 30)),
        ("2025-01", date(2025, 1, 1), date(2025, 1, 31)),
        # Mixed case
        ("APRIL 2026", date(2026, 4, 1), date(2026, 4, 30)),
        ("april 2026", date(2026, 4, 1), date(2026, 4, 30)),
    ],
)
def test_parse_month_happy_path(value, expected_start, expected_end):
    start, end = parse_month(value)
    assert start == expected_start
    assert end == expected_end


# ---------------------------------------------------------------------------
# Edge cases: month-length variations.
# ---------------------------------------------------------------------------


def test_february_leap_year_has_29_days():
    start, end = parse_month("February 2024")
    assert start == date(2024, 2, 1)
    assert end == date(2024, 2, 29)


def test_february_non_leap_year_has_28_days():
    start, end = parse_month("February 2023")
    assert start == date(2023, 2, 1)
    assert end == date(2023, 2, 28)


def test_century_year_not_leap_year():
    # 1900 was not a leap year.
    start, end = parse_month("February 1900")
    assert end == date(1900, 2, 28)


def test_2000_is_leap():
    # 2000 was a leap year.
    start, end = parse_month("February 2000")
    assert end == date(2000, 2, 29)


def test_30_day_month():
    _, end = parse_month("September 2025")
    assert end == date(2025, 9, 30)


def test_31_day_month():
    _, end = parse_month("July 2025")
    assert end == date(2025, 7, 31)


def test_december_year_boundary():
    # December's last day shouldn't accidentally roll into January.
    start, end = parse_month("December 2025")
    assert start == date(2025, 12, 1)
    assert end == date(2025, 12, 31)


# ---------------------------------------------------------------------------
# Stability: result must not depend on what day of the month it is today.
# This is the "today-default" footgun — dateutil fills missing components
# from the current date by default, so without a sentinel default,
# parse_month("April 2026") would return (April 5, April 30) on May 5.
# ---------------------------------------------------------------------------


def test_result_does_not_depend_on_current_day():
    # Calling twice should give identical results regardless of when run.
    first = parse_month("January 2025")
    second = parse_month("January 2025")
    assert first == second


def test_start_is_always_first_of_month():
    # Hits multiple months to ensure the day-1 default holds across them.
    for month_str, expected_year, expected_month in [
        ("January 2025", 2025, 1),
        ("April 2026", 2026, 4),
        ("July 2025", 2025, 7),
        ("December 2024", 2024, 12),
    ]:
        start, _ = parse_month(month_str)
        assert start.day == 1
        assert start.year == expected_year
        assert start.month == expected_month


# ---------------------------------------------------------------------------
# Failure paths: bad input rejected with ArgumentTypeError.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "not a date",
        "Decembruary 2026",
        "Smarch",
        "",
    ],
)
def test_unparseable_input_raises(value):
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        parse_month(value)
    # Error message should mention the input and suggest valid formats.
    assert repr(value) in str(exc_info.value) or value in str(exc_info.value)


def test_far_future_month_rejected():
    # A date so far in the future it's guaranteed to be after any
    # reasonable test run date.
    with pytest.raises(argparse.ArgumentTypeError) as exc_info:
        parse_month("December 2099")
    assert "future" in str(exc_info.value).lower()


def test_far_future_year_only_rejected():
    # Bare year defaults to January 1 of that year (per dateutil).
    # Should still be flagged as future.
    with pytest.raises(argparse.ArgumentTypeError):
        parse_month("2099")
