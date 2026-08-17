"""Tests for cli argparse callbacks, build_arg_parser, check_period_has_ended, main, and the console script."""

import importlib.metadata
from datetime import date
from unittest.mock import patch

import pytest

from ad_voting_metrics.cli import (
    build_arg_parser,
    check_period_has_ended,
    main,
)
from ad_voting_metrics.period import MonthPeriod

CONSOLE_SCRIPT_NAME = "ad-voting-metrics"

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


def test_parser_parses_month_and_rebuild():
    """The parser takes --month and --rebuild."""
    parser = build_arg_parser()
    args = parser.parse_args(["--month", "April 2026", "--rebuild"])

    assert isinstance(args.month, MonthPeriod)
    assert args.month.year == 2026
    assert args.month.month == 4
    assert args.rebuild is True


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


def test_main_runs_fetch(monkeypatch):
    """main(['--month', ...]) runs the fetch pipeline with the parsed period."""
    monkeypatch.setattr("ad_voting_metrics.cli.check_period_has_ended", lambda *_, **__: None)

    with patch("ad_voting_metrics.cli.run_fetch") as fetch_mock:
        main(["--month", "2026-04"])

    fetch_mock.assert_called_once()
    args = fetch_mock.call_args.args[0]
    assert args.month == MonthPeriod(year=2026, month=4)
    assert args.rebuild is False


# ---------------------------------------------------------------------------
# Console script — [project.scripts] in pyproject.toml
# ---------------------------------------------------------------------------


def test_console_script_is_installed_and_loads_main():
    """The `ad-voting-metrics` console script resolves to cli.main."""
    scripts = importlib.metadata.entry_points(group="console_scripts")
    matching = [e for e in scripts if e.name == CONSOLE_SCRIPT_NAME]
    assert matching, f"no console_scripts entry named {CONSOLE_SCRIPT_NAME!r}; re-sync the environment"

    entry = matching[0]
    assert entry.value == "ad_voting_metrics.cli:main"
    assert entry.load() is main
