"""Tests for cli argparse callbacks (other than parse_month) which lives
in test_period.py."""

import argparse
from datetime import date

import pytest

from ad_voting_metrics.cli import check_period_has_ended, parse_cache_hours
from ad_voting_metrics.period import MonthPeriod


def test_parse_cache_hours():
    assert parse_cache_hours("0") == 0


def test_parse_cache_hours_accepts_positive_integers():
    assert parse_cache_hours("24") == 24


def test_parse_cache_hours_rejects_negative():
    with pytest.raises(argparse.ArgumentTypeError, match="negative"):
        parse_cache_hours("-1")


def test_parse_cache_hours_rejects_non_integer():
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        parse_cache_hours("twelve")


def test_parse_cache_hours_rejects_float():
    with pytest.raises(argparse.ArgumentTypeError, match="not an integer"):
        parse_cache_hours("12.5")


# ---------------------------------------------------------------------------
# check_period_has_ended
# ---------------------------------------------------------------------------


def test_check_period_has_ended_accepts_period_in_past():
    """Period ended months ago: no error, returns None."""
    period = MonthPeriod(year=2026, month=1)  # ends 2026-01-31
    # No exception expected
    assert check_period_has_ended(period, today=date(2026, 5, 15)) is None


def test_check_period_has_ended_accepts_period_ending_yesterday():
    """The minimal valid case: period ended yesterday, today is the next
    day. April ends Apr 30; running on May 1 should be fine."""
    period = MonthPeriod(year=2026, month=4)
    assert check_period_has_ended(period, today=date(2026, 5, 1)) is None


def test_check_period_has_ended_rejects_period_ending_today():
    """Period boundary: today is the last day of the period. The period
    has not yet "ended" — still in progress. Refuse."""
    period = MonthPeriod(year=2026, month=4)  # ends 2026-04-30
    with pytest.raises(SystemExit, match="has not yet ended"):
        check_period_has_ended(period, today=date(2026, 4, 30))


def test_check_period_has_ended_rejects_period_in_progress():
    """Running mid-period: clearly in progress, refuse with clear message."""
    period = MonthPeriod(year=2026, month=4)
    with pytest.raises(SystemExit, match="has not yet ended"):
        check_period_has_ended(period, today=date(2026, 4, 15))


def test_check_period_has_ended_rejects_future_period():
    """Period entirely in the future."""
    period = MonthPeriod(year=2027, month=1)
    with pytest.raises(SystemExit, match="has not yet ended"):
        check_period_has_ended(period, today=date(2026, 5, 15))


def test_check_period_has_ended_message_includes_next_valid_date():
    """The error message tells the operator when they can re-run."""
    period = MonthPeriod(year=2026, month=4)
    with pytest.raises(SystemExit, match="2026-05-01"):
        check_period_has_ended(period, today=date(2026, 4, 20))


def test_check_period_has_ended_handles_year_boundary():
    """December → January next year. Make sure the next_day arithmetic
    doesn't trip on month/year overflow."""
    period = MonthPeriod(year=2026, month=12)  # ends 2026-12-31
    with pytest.raises(SystemExit, match="2027-01-01"):
        check_period_has_ended(period, today=date(2026, 12, 31))


def test_check_period_has_ended_handles_short_month():
    """February (28 days in 2026) - make sure the period.end calculation
    we depend on doesn't accidentally use 30/31."""
    period = MonthPeriod(year=2026, month=2)  # ends 2026-02-28
    # Should accept March 1
    assert check_period_has_ended(period, today=date(2026, 3, 1)) is None
    # Should reject Feb 28 (last day of period, not yet ended)
    with pytest.raises(SystemExit, match="2026-03-01"):
        check_period_has_ended(period, today=date(2026, 2, 28))


def test_check_period_has_ended_handles_leap_year():
    """February 2024 had 29 days."""
    period = MonthPeriod(year=2024, month=2)  # ends 2024-02-29
    assert check_period_has_ended(period, today=date(2024, 3, 1)) is None
    with pytest.raises(SystemExit, match="2024-03-01"):
        check_period_has_ended(period, today=date(2024, 2, 29))
