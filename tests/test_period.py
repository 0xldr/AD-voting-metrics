"""Tests for MonthPeriod."""

from datetime import date

import pytest

from ad_voting_metrics.period import MonthPeriod


@pytest.mark.parametrize("month", [13, 0, -1, 24])
def test_out_of_range_month_rejected(month):
    with pytest.raises(ValueError, match=r"month must be in 1\.\.12"):
        MonthPeriod(year=2026, month=month)


def test_unreasonable_year_rejected():
    with pytest.raises(ValueError, match="year must be"):
        MonthPeriod(year=1800, month=4)


@pytest.mark.parametrize(
    ("year", "month", "expected_end_day"),
    [
        (2025, 9, 30),  # 30-day month
        (2025, 7, 31),  # 31-day month
        (2024, 2, 29),  # leap year
        (2025, 2, 28),  # non-leap year
        (2100, 2, 28),  # century year not divisible by 400
        (2025, 12, 31),  # December doesn't roll to January
    ],
)
def test_end_day_for_calendar_variants(year, month, expected_end_day):
    p = MonthPeriod(year, month)
    assert p.end == date(year, month, expected_end_day)
    assert p.start == date(year, month, 1)


def test_str_format():
    assert str(MonthPeriod(2026, 4)) == "April 2026"
    assert str(MonthPeriod(2025, 1)) == "January 2025"
    assert str(MonthPeriod(2024, 12)) == "December 2024"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("April 2026", MonthPeriod(2026, 4)),
        ("January 2025", MonthPeriod(2025, 1)),
        ("Apr 2026", MonthPeriod(2026, 4)),
        ("2026-04", MonthPeriod(2026, 4)),
        ("APRIL 2026", MonthPeriod(2026, 4)),
        ("april 2026", MonthPeriod(2026, 4)),
    ],
)
def test_from_string_happy_path(value, expected):
    assert MonthPeriod.from_string(value) == expected


@pytest.mark.parametrize("value", ["not a date", "Decembruary 2026", ""])
def test_from_string_unparseable_raises_value_error(value):
    with pytest.raises(ValueError, match="could not parse"):
        MonthPeriod.from_string(value)
