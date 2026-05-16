"""Tests for cli argparse callbacks other than parse_month, which lives in test_period.py."""

import argparse
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from ad_voting_metrics.cli import (
    _run_finalize,
    _window_start_for_period,
    build_arg_parser,
    check_period_has_ended,
    parse_cache_hours,
)
from ad_voting_metrics.compensation import CompensationConfig, PeriodCompensation
from ad_voting_metrics.eligibility import DailyEligibility
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
# _window_start_for_period
# ---------------------------------------------------------------------------


def test_window_start_for_period_april_2026():
    """April 2026 → November 1, 2025 (6 calendar months back)."""
    assert _window_start_for_period(MonthPeriod(year=2026, month=4)) == date(2025, 11, 1)


def test_window_start_for_period_january_2026():
    """January 2026 → August 1, 2025 (rolls back across year boundary)."""
    assert _window_start_for_period(MonthPeriod(year=2026, month=1)) == date(2025, 8, 1)


def test_window_start_for_period_june_2026():
    """June 2026 → January 1, 2026 (same year)."""
    assert _window_start_for_period(MonthPeriod(year=2026, month=6)) == date(2026, 1, 1)


# ---------------------------------------------------------------------------
# _run_finalize — orchestration
# ---------------------------------------------------------------------------


def _make_finalize_args(month: MonthPeriod | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        command="finalize",
        month=month or MonthPeriod(year=2026, month=4),
    )


def test_run_finalize_workbook_open_failure_exits():
    with (
        patch(
            "ad_voting_metrics.cli.sheets.get_workbook", side_effect=RuntimeError("auth failure")
        ),
        pytest.raises(SystemExit),
    ):
        _run_finalize(_make_finalize_args())


def test_run_finalize_config_missing_exits():
    workbook = MagicMock()
    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch(
            "ad_voting_metrics.cli.sheets.read_config",
            side_effect=RuntimeError("Config tab missing"),
        ),
        pytest.raises(SystemExit),
    ):
        _run_finalize(_make_finalize_args())


def test_run_finalize_daily_data_missing_exits():
    """Case A: Daily Data tab has no rows for the period → SystemExit."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)

    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.cli.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.cli.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.cli.sheets.read_daily_data",
            side_effect=RuntimeError("no rows for April 2026"),
        ),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=[],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        with pytest.raises(SystemExit):
            _run_finalize(_make_finalize_args())


def test_run_finalize_communication_master_missing_exits():
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)

    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.cli.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.cli.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.cli.sheets.read_daily_data", return_value={date(2026, 4, 1): {}}),
        patch("ad_voting_metrics.cli.sheets.read_participation_for_window", return_value={}),
        patch(
            "ad_voting_metrics.cli.sheets.read_communication_master",
            side_effect=RuntimeError("Communication Master missing"),
        ),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=[],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        with pytest.raises(SystemExit):
            _run_finalize(_make_finalize_args())


def test_run_finalize_compensation_write_failure_exits():
    """A failed write_compensation_tab surfaces as SystemExit."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)

    # Empty roster: no delegates → eligibility produces a DailyEligibility
    # with per_delegate={} per day; compensation produces an empty
    # PeriodCompensation. Write step then fails (mocked) → exit.
    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.cli.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.cli.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.cli.sheets.read_daily_data",
            return_value={date(2026, 4, d): {} for d in range(1, 31)},
        ),
        patch("ad_voting_metrics.cli.sheets.read_participation_for_window", return_value={}),
        patch("ad_voting_metrics.cli.sheets.read_communication_master", return_value={}),
        patch(
            "ad_voting_metrics.cli.sheets.write_compensation_tab",
            side_effect=RuntimeError("API error"),
        ),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=[],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        with pytest.raises(SystemExit):
            _run_finalize(_make_finalize_args(period))


def test_run_finalize_happy_path_empty_roster_writes_comp_tab():
    """Smallest happy path: empty roster, comp tab written successfully."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)

    write_mock = MagicMock(return_value=MagicMock())
    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.cli.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.cli.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.cli.sheets.read_daily_data",
            return_value={date(2026, 4, d): {} for d in range(1, 31)},
        ),
        patch("ad_voting_metrics.cli.sheets.read_participation_for_window", return_value={}),
        patch("ad_voting_metrics.cli.sheets.read_communication_master", return_value={}),
        patch("ad_voting_metrics.cli.sheets.write_compensation_tab", write_mock),
        patch("ad_voting_metrics.cli.write_entry"),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=[],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        _run_finalize(_make_finalize_args(period))

    # write_compensation_tab was called with the workbook and a
    # PeriodCompensation. Empty roster → no per_delegate rows.
    write_mock.assert_called_once()
    _, period_comp = write_mock.call_args.args
    assert isinstance(period_comp, PeriodCompensation)
    assert period_comp.period == period
    assert period_comp.config == config
    assert period_comp.per_delegate == []


def test_run_finalize_eligibility_tie_at_cutoff_exits():
    """A tie at the L3 slot cutoff propagates as SystemExit."""
    # We need a roster of 7 delegates all tied at the L3 cutoff. The
    # easiest construction: roster of 7 with same rank, no L1/L2.
    from ad_voting_metrics.roster import Delegate

    delegates = [
        Delegate(
            name=f"D{i}",
            vote_delegate_address=f"0x{'a' * 39}{i}",
            start_date=date(2024, 1, 1),
        )
        for i in range(7)
    ]
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)
    # All ranked 6 → tie crossing the 6-slot cutoff
    ranks_per_day = {date(2026, 4, d): {f"D{i}": 6 for i in range(7)} for d in range(1, 31)}
    # Give everyone perfect participation history so they're all eligible
    participation = {
        f"D{i}": [(f"poll_{j}", date(2026, 2, 1), "Yes") for j in range(10)] for i in range(7)
    }
    communication = {f"D{i}": {f"poll_{j}": "Yes" for j in range(10)} for i in range(7)}

    with (
        patch("ad_voting_metrics.cli.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.cli.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.cli.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.cli.sheets.read_daily_data", return_value=ranks_per_day),
        patch(
            "ad_voting_metrics.cli.sheets.read_participation_for_window", return_value=participation
        ),
        patch("ad_voting_metrics.cli.sheets.read_communication_master", return_value=communication),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=delegates,
            drift_warnings=[],
            yaml_config=MagicMock(delegates=delegates),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        with pytest.raises(SystemExit):
            _run_finalize(_make_finalize_args(period))
