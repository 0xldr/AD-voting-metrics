"""Tests for pipeline.run_fetch / pipeline.run_finalize and their helpers."""

import argparse
from datetime import date
from unittest.mock import MagicMock, patch

import gspread
import pandas as pd
import pytest

from ad_voting_metrics import pipeline
from ad_voting_metrics.compensation import CompensationConfig, PeriodCompensation
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.pipeline import (
    _build_sky_and_ranking_frames,
    _window_start_for_period,
    _write_fetch_csvs,
    _write_fetch_workbook_tabs,
    run_fetch,
    run_finalize,
)
from ad_voting_metrics.roster import Delegate

# ---------------------------------------------------------------------------
# _window_start_for_period
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("period_year", "period_month", "expected"),
    [
        (2026, 4, date(2025, 11, 1)),  # rolls back across year boundary
        (2026, 1, date(2025, 8, 1)),  # rolls back further
        (2026, 6, date(2026, 1, 1)),  # same year
    ],
)
def test_window_start_for_period(period_year, period_month, expected):
    assert _window_start_for_period(MonthPeriod(year=period_year, month=period_month)) == expected


# ---------------------------------------------------------------------------
# run_finalize — orchestration
# ---------------------------------------------------------------------------


def _make_finalize_args(month: MonthPeriod | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        command="finalize",
        month=month or MonthPeriod(year=2026, month=4),
    )


def test_run_finalize_workbook_open_failure_exits():
    with (
        patch(
            "ad_voting_metrics.pipeline.sheets.get_workbook",
            side_effect=RuntimeError("auth failure"),
        ),
        pytest.raises(SystemExit),
    ):
        run_finalize(_make_finalize_args())


def test_run_finalize_config_missing_exits():
    workbook = MagicMock()
    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch(
            "ad_voting_metrics.pipeline.sheets.read_config",
            side_effect=RuntimeError("Config tab missing"),
        ),
        pytest.raises(SystemExit),
    ):
        run_finalize(_make_finalize_args())


def test_run_finalize_daily_data_missing_exits():
    """Case A: Daily Data tab has no rows for the period → SystemExit."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.pipeline.sheets.read_daily_data",
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
            run_finalize(_make_finalize_args())


def test_run_finalize_communication_master_missing_exits():
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.sheets.read_daily_data", return_value={date(2026, 4, 1): {}}),
        patch("ad_voting_metrics.pipeline.sheets.read_participation_for_window", return_value={}),
        patch(
            "ad_voting_metrics.pipeline.sheets.read_communication_master",
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
            run_finalize(_make_finalize_args())


def test_run_finalize_compensation_write_failure_exits():
    """A failed write_compensation_tab surfaces as SystemExit."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)

    # Empty roster: no delegates → eligibility produces a DailyEligibility
    # with per_delegate={} per day; compensation produces an empty
    # PeriodCompensation. Write step then fails (mocked) → exit.
    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.pipeline.sheets.read_daily_data",
            return_value={date(2026, 4, d): {} for d in range(1, 31)},
        ),
        patch("ad_voting_metrics.pipeline.sheets.read_participation_for_window", return_value={}),
        patch("ad_voting_metrics.pipeline.sheets.read_communication_master", return_value={}),
        patch(
            "ad_voting_metrics.pipeline.sheets.write_compensation_tab",
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
            run_finalize(_make_finalize_args(period))


def test_run_finalize_happy_path_empty_roster_writes_comp_tab():
    """Smallest happy path: empty roster, comp tab written successfully."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)

    write_mock = MagicMock(return_value=MagicMock())
    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.pipeline.sheets.read_daily_data",
            return_value={date(2026, 4, d): {} for d in range(1, 31)},
        ),
        patch("ad_voting_metrics.pipeline.sheets.read_participation_for_window", return_value={}),
        patch("ad_voting_metrics.pipeline.sheets.read_communication_master", return_value={}),
        patch("ad_voting_metrics.pipeline.sheets.write_compensation_tab", write_mock),
        patch("ad_voting_metrics.pipeline.write_entry"),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=[],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        run_finalize(_make_finalize_args(period))

    # write_compensation_tab was called with the workbook and a
    # PeriodCompensation. Empty roster → no per_delegate rows.
    write_mock.assert_called_once()
    _, period_comp = write_mock.call_args.args
    assert isinstance(period_comp, PeriodCompensation)
    assert period_comp.period == period
    assert period_comp.config == config
    assert period_comp.per_delegate == []

    # Verify finalize asks roster to skip the API drift check
    mock_roster.assert_called_once()
    assert mock_roster.call_args.kwargs.get("skip_api_check") is True


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
    participation = {f"D{i}": [(f"poll_{j}", date(2026, 2, 1), "Yes") for j in range(10)] for i in range(7)}
    communication = {f"D{i}": {f"poll_{j}": "Yes" for j in range(10)} for i in range(7)}

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.sheets.read_daily_data", return_value=ranks_per_day),
        patch(
            "ad_voting_metrics.pipeline.sheets.read_participation_for_window",
            return_value=participation,
        ),
        patch("ad_voting_metrics.pipeline.sheets.read_communication_master", return_value=communication),
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=delegates,
            drift_warnings=[],
            yaml_config=MagicMock(delegates=delegates),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        with pytest.raises(SystemExit):
            run_finalize(_make_finalize_args(period))


def test_run_finalize_logs_drift_warnings():
    """Drift warnings from the roster are emitted via logger.warning."""
    workbook = MagicMock()
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    period = MonthPeriod(year=2026, month=4)

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=config),
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch(
            "ad_voting_metrics.pipeline.sheets.read_daily_data",
            return_value={date(2026, 4, d): {} for d in range(1, 31)},
        ),
        patch("ad_voting_metrics.pipeline.sheets.read_participation_for_window", return_value={}),
        patch("ad_voting_metrics.pipeline.sheets.read_communication_master", return_value={}),
        patch("ad_voting_metrics.pipeline.sheets.write_compensation_tab"),
        patch("ad_voting_metrics.pipeline.write_entry"),
        patch.object(pipeline.logger, "warning") as warning_mock,
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=[],
            drift_warnings=["YAML/API drift: delegate X missing from API"],
            yaml_config=MagicMock(delegates=[]),
            api_delegate_count=0,
            api_fetch_succeeded=True,
        )
        run_finalize(_make_finalize_args(period))

    warning_mock.assert_any_call("YAML/API drift: delegate X missing from API")


# ---------------------------------------------------------------------------
# _build_sky_and_ranking_frames — pure Dune-output transform
# ---------------------------------------------------------------------------


def test_build_sky_and_ranking_frames_sorts_sky_and_ranks_delegates():
    """df_sky sorted by (date, sky, contract) desc; df_ranking has Rank per date."""
    period = MonthPeriod(year=2026, month=4)
    canned = pd.DataFrame([
        {"contract": "0xa", "name": "alpha", "date": date(2026, 4, 1), "sky": 100.0},
        {"contract": "0xb", "name": "beta", "date": date(2026, 4, 1), "sky": 300.0},
        {"contract": "0xc", "name": "gamma", "date": date(2026, 4, 1), "sky": 200.0},
    ])
    df_input = pd.DataFrame({
        "Delegate Name": ["alpha", "beta", "gamma"],
        "Delegate Contract": ["0xa", "0xb", "0xc"],
        "Start Date": ["2024-01-01"] * 3,
    })

    with patch(
        "ad_voting_metrics.pipeline.dune.get_delegate_list_sky",
        return_value=canned,
    ) as dune_mock:
        df_sky, df_ranking = _build_sky_and_ranking_frames(df_input, period, cache_hours=24)

    dune_mock.assert_called_once_with(df_input, period, cache_max_age_hours=24)

    # df_sky: highest sky first (300), then 200, then 100.
    assert df_sky.iloc[0]["sky"] == 300.0
    assert df_sky.iloc[-1]["sky"] == 100.0

    # df_ranking: ranks computed from Total Delegation desc per Date.
    ranks_by_delegate = df_ranking.set_index("Delegate")["Rank"].to_dict()
    assert ranks_by_delegate == {"beta": 1, "gamma": 2, "alpha": 3}


# ---------------------------------------------------------------------------
# _write_fetch_csvs — file IO to OUTPUT_DIR
# ---------------------------------------------------------------------------


def test_write_fetch_csvs_writes_both_files_and_returns_paths(tmp_path, monkeypatch):
    """Writes sky.csv and vote_participation.csv to OUTPUT_DIR; returns both paths in order."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)

    df_sky = pd.DataFrame([{"contract": "0xa", "date": date(2026, 4, 1), "sky": 100.0}])
    df = pd.DataFrame({
        "Delegate Name": ["alpha"],
        "Delegate Contract": ["0xa"],
        "Start Date": ["2024-01-01"],
        "101": ["Yes"],
    })
    poll_info = [{"pollId": 101, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3), "title": "Test"}]
    spell_info: list[dict] = []

    result = _write_fetch_csvs(df, df_sky, poll_info, spell_info)

    assert [p.name for p in result] == ["sky.csv", "vote_participation.csv"]
    assert all(p.exists() for p in result)
    assert (tmp_path / "sky.csv").read_text().splitlines()[0] == "contract,date,sky"


# ---------------------------------------------------------------------------
# _write_fetch_workbook_tabs — sheets writer orchestration
# ---------------------------------------------------------------------------


def test_write_fetch_workbook_tabs_calls_three_writers_on_success():
    """All three tab writers are invoked when get_workbook succeeds."""
    period = MonthPeriod(year=2026, month=4)
    workbook = MagicMock()

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.write_participation_raw_data") as p_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_fetch_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    p_mock.assert_called_once()
    c_mock.assert_called_once()
    d_mock.assert_called_once()


def test_write_fetch_workbook_tabs_skips_tabs_when_workbook_open_fails():
    """If get_workbook raises RuntimeError, no tab writer runs and no exception propagates."""
    period = MonthPeriod(year=2026, month=4)

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", side_effect=RuntimeError("auth")),
        patch("ad_voting_metrics.pipeline.sheets.write_participation_raw_data") as p_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_fetch_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    p_mock.assert_not_called()
    c_mock.assert_not_called()
    d_mock.assert_not_called()


def test_write_fetch_workbook_tabs_continues_after_value_error():
    """A ValueError from one writer is logged; remaining writers still run."""
    period = MonthPeriod(year=2026, month=4)
    workbook = MagicMock()

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch(
            "ad_voting_metrics.pipeline.sheets.write_participation_raw_data",
            side_effect=ValueError("bad df"),
        ),
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_fetch_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    c_mock.assert_called_once()
    d_mock.assert_called_once()


def test_write_fetch_workbook_tabs_continues_after_gspread_api_error():
    """A gspread APIError from a writer is logged; remaining writers still run."""
    period = MonthPeriod(year=2026, month=4)
    workbook = MagicMock()
    # APIError's constructor takes a response; mimic the bits the formatter touches.
    api_error = gspread.exceptions.APIError(MagicMock(status_code=500, json=dict))

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch(
            "ad_voting_metrics.pipeline.sheets.write_participation_raw_data",
            side_effect=api_error,
        ),
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_fetch_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    c_mock.assert_called_once()
    d_mock.assert_called_once()


# ---------------------------------------------------------------------------
# run_fetch — end-to-end orchestration with everything mocked
# ---------------------------------------------------------------------------


def _make_fetch_args(
    month: MonthPeriod | None = None,
    cache_hours: int | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="fetch",
        month=month or MonthPeriod(year=2026, month=4),
        cache_hours=cache_hours,
    )


def _canned_dune_outputs(period: MonthPeriod, contract: str, name: str) -> pd.DataFrame:
    """Return a sky_protocol-shaped DataFrame covering one delegate for every day in period."""
    days = list(pd.date_range(period.start, period.end, freq="D").date)
    return pd.DataFrame([
        {"contract": contract, "name": name, "date": d, "sky": 100.0} for d in days
    ])


def test_run_fetch_writes_csvs_and_workbook_tabs(tmp_path, monkeypatch):
    """End-to-end fetch with all externals mocked → CSVs + 3 workbook tabs written."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "RECONCILIATION_LOG_PATH", tmp_path / "rec")

    period = MonthPeriod(year=2026, month=4)
    contract = "0x" + "a" * 40
    delegates = [
        Delegate(name="alpha", vote_delegate_address=contract, start_date=date(2024, 1, 1)),
    ]
    canned = _canned_dune_outputs(period, contract, "alpha")

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.dune.get_delegate_list_sky", return_value=canned),
        patch("ad_voting_metrics.pipeline.sky_polling.get_poll_ids", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.get_executive_ids", return_value=[]),
        patch(
            "ad_voting_metrics.pipeline.sky_polling.get_vote_poll_ids",
            side_effect=lambda _poll_info, df, _df_sky, current_datetime: df,  # noqa: ARG005
        ),
        patch(
            "ad_voting_metrics.pipeline.sky_executive.get_vote_executive_ids",
            side_effect=lambda _spell_info, df, _df_sky: df,
        ),
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=MagicMock()),
        patch("ad_voting_metrics.pipeline.sheets.write_participation_raw_data") as p_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
        patch("ad_voting_metrics.pipeline.write_entry") as entry_mock,
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=delegates,
            drift_warnings=[],
            yaml_config=MagicMock(delegates=delegates),
            api_delegate_count=1,
            api_fetch_succeeded=True,
        )
        run_fetch(_make_fetch_args(period, cache_hours=24))

    assert (tmp_path / "sky.csv").exists()
    assert (tmp_path / "vote_participation.csv").exists()
    p_mock.assert_called_once()
    c_mock.assert_called_once()
    d_mock.assert_called_once()
    entry_mock.assert_called_once()


def test_run_fetch_logs_drift_warnings(tmp_path, monkeypatch):
    """Drift warnings on the roster surface through logger.warning."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "RECONCILIATION_LOG_PATH", tmp_path / "rec")

    period = MonthPeriod(year=2026, month=4)
    contract = "0x" + "b" * 40
    delegates = [
        Delegate(name="beta", vote_delegate_address=contract, start_date=date(2024, 1, 1)),
    ]
    canned = _canned_dune_outputs(period, contract, "beta")

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.dune.get_delegate_list_sky", return_value=canned),
        patch("ad_voting_metrics.pipeline.sky_polling.get_poll_ids", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.get_executive_ids", return_value=[]),
        patch(
            "ad_voting_metrics.pipeline.sky_polling.get_vote_poll_ids",
            side_effect=lambda _poll_info, df, _df_sky, current_datetime: df,  # noqa: ARG005
        ),
        patch(
            "ad_voting_metrics.pipeline.sky_executive.get_vote_executive_ids",
            side_effect=lambda _spell_info, df, _df_sky: df,
        ),
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", side_effect=RuntimeError("no workbook")),
        patch("ad_voting_metrics.pipeline.write_entry"),
        patch.object(pipeline.logger, "warning") as warning_mock,
    ):
        mock_roster.return_value = MagicMock(
            active_delegates=delegates,
            drift_warnings=["YAML lists Z but API doesn't"],
            yaml_config=MagicMock(delegates=delegates),
            api_delegate_count=1,
            api_fetch_succeeded=True,
        )
        run_fetch(_make_fetch_args(period))

    warning_mock.assert_any_call("YAML lists Z but API doesn't")
