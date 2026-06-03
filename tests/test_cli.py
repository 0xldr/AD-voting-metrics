"""Tests for cli argparse callbacks, build_arg_parser, check_period_has_ended, and main dispatch."""

from datetime import date
from unittest.mock import patch

import pytest

from ad_voting_metrics.cli import (
    build_arg_parser,
    check_period_has_ended,
    main,
)
from ad_voting_metrics.period import MonthPeriod

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


def test_parser_fetch_subcommand_parses_month_and_rebuild():
    """The fetch subcommand takes --month and --rebuild."""
    parser = build_arg_parser()
    args = parser.parse_args(["fetch", "--month", "April 2026", "--rebuild"])

    assert args.command == "fetch"
    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4
    assert args.rebuild is True


def test_parser_fetch_subcommand_rebuild_defaults_to_false():
    """--rebuild defaults to False when omitted."""
    parser = build_arg_parser()
    args = parser.parse_args(["fetch", "--month", "April 2026"])

    assert args.command == "fetch"
    assert args.rebuild is False


def test_parser_finalize_subcommand_parses_month():
    """The finalize subcommand takes --month."""
    parser = build_arg_parser()
    args = parser.parse_args(["finalize", "--month", "April 2026"])

    assert args.command == "finalize"
    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4


# ---------------------------------------------------------------------------
# main() — argparse + dispatch
# ---------------------------------------------------------------------------


def test_main_dispatches_fetch_subcommand(monkeypatch):
    """main(['fetch', '--month', ...]) routes to run_fetch."""
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", lambda *_, **__: None)

    with (
        patch("ad_voting_metrics.cli.run_fetch") as fetch_mock,
        patch("ad_voting_metrics.cli.run_finalize") as finalize_mock,
    ):
        main(["fetch", "--month", "2026-04"])

    fetch_mock.assert_called_once()
    finalize_mock.assert_not_called()


def test_main_dispatches_finalize_subcommand(monkeypatch):
    """main(['finalize', '--month', ...]) routes to run_finalize."""
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", lambda *_, **__: None)

    with (
        patch("ad_voting_metrics.cli.run_fetch") as fetch_mock,
        patch("ad_voting_metrics.cli.run_finalize") as finalize_mock,
    ):
        main(["finalize", "--month", "2026-04"])

    finalize_mock.assert_called_once()
    fetch_mock.assert_not_called()
