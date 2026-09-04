"""Tests for pipeline.run and its ranking helper."""

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from web3.exceptions import Web3Exception

from ad_voting_metrics import pipeline
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.pipeline import _rank_daily_balances, run
from ad_voting_metrics.roster import Delegate

# ---------------------------------------------------------------------------
# _rank_daily_balances
# ---------------------------------------------------------------------------


def test_rank_daily_balances_ranks_within_each_day_and_sorts_by_date_then_rank():
    daily = pd.DataFrame(
        [
            {"contract": "0xa", "name": "alpha", "date": date(2026, 4, 2), "sky": 100.0},
            {"contract": "0xb", "name": "beta", "date": date(2026, 4, 1), "sky": 300.0},
            {"contract": "0xa", "name": "alpha", "date": date(2026, 4, 1), "sky": 200.0},
            {"contract": "0xb", "name": "beta", "date": date(2026, 4, 2), "sky": 50.0},
        ]
    )

    out = _rank_daily_balances(daily)

    assert list(out.columns) == ["contract", "name", "date", "sky", "rank"]
    assert list(zip(out["date"], out["name"], out["rank"], strict=True)) == [
        (date(2026, 4, 1), "beta", 1),
        (date(2026, 4, 1), "alpha", 2),
        (date(2026, 4, 2), "alpha", 1),
        (date(2026, 4, 2), "beta", 2),
    ]


def test_rank_daily_balances_breaks_ties_by_row_order():
    daily = pd.DataFrame(
        [
            {"contract": "0xa", "name": "alpha", "date": date(2026, 4, 1), "sky": 100.0},
            {"contract": "0xb", "name": "beta", "date": date(2026, 4, 1), "sky": 100.0},
        ]
    )

    assert list(_rank_daily_balances(daily)["rank"]) == [1, 2]


# ---------------------------------------------------------------------------
# run — end-to-end orchestration with every external call mocked
# ---------------------------------------------------------------------------


@pytest.fixture
def externals(tmp_path):
    """Patch every external call made by run() for one delegate over April 2026.

    Yields a namespace with the keyword arguments for run(), the month's output directory, the roster mock (so tests
    can shape drift warnings), the delegation mock, and the write_entry mock.
    """
    period = MonthPeriod(year=2026, month=4)
    contract = "0x" + "a" * 40
    delegates = [Delegate(name="alpha", vote_delegate_address=contract, start_date=date(2024, 1, 1))]
    days = pd.date_range(period.start, period.end, freq="D").date
    daily = pd.DataFrame([{"contract": contract, "name": "alpha", "date": d, "sky": 100.0} for d in days])
    roster = MagicMock(
        active_delegates=delegates,
        drift_warnings=[],
        yaml_config=MagicMock(delegates=delegates),
        api_delegate_count=1,
        api_fetch_succeeded=True,
    )

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period", return_value=roster),
        patch("ad_voting_metrics.pipeline.delegation.get_delegate_list_sky", return_value=daily) as delegation_mock,
        patch("ad_voting_metrics.pipeline.sky_polling.fetch_polls_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.fetch_spells_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_polling.add_poll_vote_statuses", side_effect=lambda _p, df, _s, **_: df),
        patch("ad_voting_metrics.pipeline.sky_executive.add_spell_vote_statuses", side_effect=lambda _s, df, _l: df),
        patch("ad_voting_metrics.pipeline.write_entry") as entry_mock,
    ):
        yield SimpleNamespace(
            period=period,
            run_kwargs={"roster_path": tmp_path / "delegates.yaml", "output_dir": tmp_path, "w3": MagicMock()},
            out_dir=tmp_path / "2026-04",
            roster=roster,
            delegation_mock=delegation_mock,
            entry_mock=entry_mock,
        )


def test_run_writes_both_csvs_into_the_month_directory(externals):
    run(externals.period, rebuild=False, **externals.run_kwargs)

    assert sorted(p.name for p in externals.out_dir.iterdir()) == ["sky.csv", "vote_participation.csv"]
    sky = pd.read_csv(externals.out_dir / "sky.csv")
    assert list(sky.columns) == ["contract", "name", "date", "sky", "rank"]
    assert len(sky) == 30
    assert set(sky["rank"]) == {1}


def test_run_threads_paths_client_and_rebuild_through_to_collaborators(externals):
    with patch(
        "ad_voting_metrics.pipeline.sky_executive_onchain.resolve_pending_executive_votes",
        side_effect=lambda df, _spells, **_: df,
    ) as onchain_mock:
        run(externals.period, rebuild=True, **externals.run_kwargs)

    kwargs = externals.delegation_mock.call_args.kwargs
    assert kwargs["rebuild"] is True
    assert kwargs["w3"] is externals.run_kwargs["w3"]
    assert kwargs["cache_path"] == externals.run_kwargs["output_dir"] / "delegation_cache.json"

    onchain_kwargs = onchain_mock.call_args.kwargs
    assert onchain_kwargs["w3"] is externals.run_kwargs["w3"]
    assert onchain_kwargs["cache_path"] == externals.run_kwargs["output_dir"] / "slate_cache.json"

    log_dir, _period, entry = externals.entry_mock.call_args.args
    assert log_dir == externals.run_kwargs["output_dir"] / "reconciliation"
    assert entry["yaml_path"] == str(externals.run_kwargs["roster_path"])
    assert [Path(p).name for p in entry["output_files"]] == ["sky.csv", "vote_participation.csv"]


def test_run_logs_drift_warnings(externals):
    externals.roster.drift_warnings = ["YAML lists Z but API doesn't"]

    with patch.object(pipeline.logger, "warning") as warning_mock:
        run(externals.period, rebuild=False, **externals.run_kwargs)

    warning_mock.assert_any_call("YAML lists Z but API doesn't")


def test_run_still_writes_outputs_when_onchain_verification_fails(externals):
    """A transient RPC failure during spell verification is logged; the run completes with cells left Pending."""
    with patch(
        "ad_voting_metrics.pipeline.sky_executive_onchain.resolve_pending_executive_votes",
        side_effect=Web3Exception("rpc down"),
    ):
        run(externals.period, rebuild=False, **externals.run_kwargs)

    assert (externals.out_dir / "vote_participation.csv").exists()
    externals.entry_mock.assert_called_once()
