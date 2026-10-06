"""Tests for pipeline.run."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from web3.exceptions import Web3Exception

from ad_voting_metrics import pipeline
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.pipeline import run
from ad_voting_metrics.sources.delegation import DelegationCache
from tests.helpers import ADDR_A, delegate


@pytest.fixture
def externals(tmp_path):
    """Patch every external call made by run() for one delegate over April 2026.

    Yields a namespace with the keyword arguments for run(), the month's output directory, the roster mock (so tests
    can shape drift warnings), the sync mock and the cache it returns, and the reconciliation-entry writer mock.
    """
    period = MonthPeriod(year=2026, month=4)
    contract = ADDR_A
    delegates = [delegate("alpha", contract)]
    days = pd.date_range(period.start, period.end, freq="D").date
    daily = pd.DataFrame([{"contract": contract, "name": "alpha", "date": d, "sky": 100.0, "rank": 1} for d in days])
    cache = DelegationCache(last_synced_block=22_500_000, block_timestamps={22_400_000: 1_700_000_000})
    roster = MagicMock(
        active_delegates=delegates,
        drift_warnings=[],
        yaml_config=MagicMock(delegates=delegates),
        api_delegate_count=1,
    )

    with (
        patch("ad_voting_metrics.pipeline.build_roster_for_period", return_value=roster),
        patch("ad_voting_metrics.pipeline.delegation.sync_events", return_value=cache) as sync_mock,
        patch("ad_voting_metrics.pipeline.delegation.daily_balances", return_value=daily),
        patch("ad_voting_metrics.pipeline.sky_polling.fetch_polls_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_executive.fetch_spells_for_period", return_value=[]),
        patch("ad_voting_metrics.pipeline.sky_polling.poll_statuses", return_value={}),
        patch("ad_voting_metrics.pipeline.sky_executive.spell_statuses", return_value={}),
        patch("ad_voting_metrics.pipeline.write_reconciliation_entry") as entry_mock,
    ):
        yield SimpleNamespace(
            period=period,
            run_kwargs={"roster_path": tmp_path / "delegates.yaml", "output_dir": tmp_path, "w3": MagicMock()},
            out_dir=tmp_path / "2026-04",
            roster=roster,
            sync_mock=sync_mock,
            cache=cache,
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
    def return_statuses_unchanged(statuses, spells, delegates, **kwargs):
        return statuses

    with patch(
        "ad_voting_metrics.pipeline.sky_executive.resolve_pending_executive_votes",
        side_effect=return_statuses_unchanged,
    ) as onchain_mock:
        run(externals.period, rebuild=True, **externals.run_kwargs)

    sync_args, sync_kwargs = externals.sync_mock.call_args
    assert sync_args[0] is externals.run_kwargs["w3"]
    assert sync_args[1] == [externals.roster.active_delegates[0].vote_delegate_address]
    assert sync_kwargs["rebuild"] is True
    assert sync_kwargs["cache_path"] == externals.run_kwargs["output_dir"] / "delegation_cache.json"

    onchain_kwargs = onchain_mock.call_args.kwargs
    assert onchain_kwargs["w3"] is externals.run_kwargs["w3"]
    assert onchain_kwargs["cache_path"] == externals.run_kwargs["output_dir"] / "slate_cache.json"
    assert onchain_kwargs["known_block_timestamps"] == externals.cache.block_timestamps

    log_dir, _period, entry = externals.entry_mock.call_args.args
    assert log_dir == externals.run_kwargs["output_dir"] / "reconciliation"
    assert entry["roster_path"] == str(externals.run_kwargs["roster_path"])
    assert entry["last_synced_block"] == externals.cache.last_synced_block
    assert [Path(p).name for p in entry["output_files"]] == ["sky.csv", "vote_participation.csv"]


def test_run_logs_drift_warnings(externals):
    externals.roster.drift_warnings = ["YAML lists Z but API doesn't"]

    with patch.object(pipeline.logger, "warning") as warning_mock:
        run(externals.period, rebuild=False, **externals.run_kwargs)

    warning_mock.assert_any_call("YAML lists Z but API doesn't")


def test_run_still_writes_outputs_when_onchain_verification_fails(externals):
    """A transient RPC failure during spell verification is logged; the run completes with cells left Pending."""
    with patch(
        "ad_voting_metrics.pipeline.sky_executive.resolve_pending_executive_votes",
        side_effect=Web3Exception("rpc down"),
    ):
        run(externals.period, rebuild=False, **externals.run_kwargs)

    assert (externals.out_dir / "vote_participation.csv").exists()
    externals.entry_mock.assert_called_once()
