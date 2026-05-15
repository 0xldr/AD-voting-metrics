"""Tests for cli argparse callbacks other than parse_month, which lives in test_period.py."""

import argparse
from datetime import date

import pytest

from ad_voting_metrics.cli import (
    _run_finalize,
    build_arg_parser,
    check_period_has_ended,
    parse_cache_hours,
)
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
    check_period_has_ended(period, today=date(2026, 5, 15))


def test_check_period_has_ended_accepts_period_ending_yesterday():
    """April ends Apr 30; running on May 1 should be fine."""
    period = MonthPeriod(year=2026, month=4)
    check_period_has_ended(period, today=date(2026, 5, 1))


def test_check_period_has_ended_rejects_period_ending_today():
    """Last day of the period: still in progress, reject."""
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
    """Verify December → January arithmetic doesn't trip on month/year overflow."""
    period = MonthPeriod(year=2026, month=12)  # ends 2026-12-31
    with pytest.raises(SystemExit, match="2027-01-01"):
        check_period_has_ended(period, today=date(2026, 12, 31))


def test_check_period_has_ended_handles_short_month():
    """Verify period.end calculation uses 28 for a 28-day February, not 30/31."""
    period = MonthPeriod(year=2026, month=2)  # ends 2026-02-28
    # Should accept March 1
    check_period_has_ended(period, today=date(2026, 3, 1))
    # Should reject Feb 28 (last day of period, not yet ended)
    with pytest.raises(SystemExit, match="2026-03-01"):
        check_period_has_ended(period, today=date(2026, 2, 28))


def test_check_period_has_ended_handles_leap_year():
    """February 2024 had 29 days."""
    period = MonthPeriod(year=2024, month=2)  # ends 2024-02-29
    check_period_has_ended(period, today=date(2024, 3, 1))
    with pytest.raises(SystemExit, match="2024-03-01"):
        check_period_has_ended(period, today=date(2024, 2, 29))


# ---------------------------------------------------------------------------
# Subcommand structure — build_arg_parser
# ---------------------------------------------------------------------------


def test_parser_fetch_subcommand_parses_month_and_cache_hours():
    """The fetch subcommand takes --month and --cache-hours."""
    parser = build_arg_parser()
    args = parser.parse_args(["fetch", "--month", "April 2026", "--cache-hours", "24"])

    assert args.command == "fetch"
    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4
    assert args.cache_hours == 24


def test_parser_fetch_subcommand_cache_hours_optional():
    """--cache-hours defaults to None when omitted."""
    parser = build_arg_parser()
    args = parser.parse_args(["fetch", "--month", "April 2026"])

    assert args.command == "fetch"
    assert args.cache_hours is None


def test_parser_finalize_subcommand_parses_month():
    """The finalize subcommand takes --month."""
    parser = build_arg_parser()
    args = parser.parse_args(["finalize", "--month", "April 2026"])

    assert args.command == "finalize"
    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4


def test_parser_finalize_does_not_accept_cache_hours():
    """--cache-hours is a fetch-only flag; argparse rejects it on finalize."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["finalize", "--month", "April 2026", "--cache-hours", "24"])


def test_parser_requires_subcommand():
    """Calling without a subcommand exits with argparse's usage error."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_unknown_subcommand_rejected():
    """A subcommand not in {fetch, finalize} exits with argparse error."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bogus", "--month", "April 2026"])


def test_parser_both_subcommands_require_month():
    """--month is required on both subcommands."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["fetch"])
    with pytest.raises(SystemExit):
        parser.parse_args(["finalize"])


# ---------------------------------------------------------------------------
# _run_finalize stub
# ---------------------------------------------------------------------------


def test_run_finalize_stub_raises_not_implemented(monkeypatch):
    """Raise SystemExit with a clear "not yet implemented" message."""
    fake_args = argparse.Namespace(
        command="finalize",
        month=MonthPeriod(year=2026, month=1),
    )
    with pytest.raises(SystemExit, match="not yet implemented"):
        _run_finalize(fake_args)
