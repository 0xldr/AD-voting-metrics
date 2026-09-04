"""Tests for cli: build_arg_parser, check_period_has_ended, and main."""

from datetime import date
from pathlib import Path
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


def test_check_period_has_ended_handles_year_boundary():
    """December → January arithmetic doesn't trip on month/year overflow.

    The day-after-period computation crossing a year boundary is the one calendar
    case worth checking here; per-month end-of-month length is covered by test_period.
    """
    period = MonthPeriod(year=2026, month=12)  # ends 2026-12-31
    with pytest.raises(SystemExit, match="2027-01-01"):
        check_period_has_ended(period, today=date(2026, 12, 31))


# ---------------------------------------------------------------------------
# build_arg_parser
# ---------------------------------------------------------------------------


def test_parser_parses_month_and_rebuild_with_default_paths():
    """The parser takes --month and --rebuild; roster and output dir default to the working directory."""
    parser = build_arg_parser()
    args = parser.parse_args(["--month", "April 2026", "--rebuild"])

    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4
    assert args.rebuild is True
    assert args.roster == Path("delegates.yaml")
    assert args.output_dir == Path("output_data")


def test_parser_accepts_roster_and_output_dir_overrides():
    parser = build_arg_parser()
    args = parser.parse_args(["--month", "April 2026", "--roster", "r.yaml", "--output-dir", "/tmp/out"])

    assert args.roster == Path("r.yaml")
    assert args.output_dir == Path("/tmp/out")


def test_parser_rebuild_defaults_to_false():
    """--rebuild defaults to False when omitted."""
    parser = build_arg_parser()
    args = parser.parse_args(["--month", "April 2026"])

    assert args.rebuild is False


def test_parser_requires_month():
    """--month is required; argparse exits with usage when it's absent."""
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# ---------------------------------------------------------------------------
# main() — argparse + run
# ---------------------------------------------------------------------------


def test_main_runs_pipeline(monkeypatch):
    """main(['--month', ...]) runs the pipeline with the parsed period and rebuild flag."""
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", lambda *_, **__: None)

    with patch("ad_voting_metrics.cli.run") as run_mock:
        main(["--month", "2026-04"])

    run_mock.assert_called_once_with(
        MonthPeriod(year=2026, month=4),
        rebuild=False,
        roster_path=Path("delegates.yaml"),
        output_dir=Path("output_data"),
    )
