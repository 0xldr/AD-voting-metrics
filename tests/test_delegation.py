"""Tests for sources.delegation — on-chain event replay and daily totals."""

import json
from datetime import date
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import delegation


def _period_stub(start: date, end: date) -> MonthPeriod:
    """Build a period-shaped stub covering [start, end].

    Tests need arbitrary date ranges; real MonthPeriod only models calendar months.
    The delegation module only reads .start/.end on the period it's handed, so a
    duck-typed SimpleNamespace stands in (cast to MonthPeriod for the type checker).
    """
    return cast("MonthPeriod", SimpleNamespace(start=start, end=end, year=start.year, month=start.month))


# ---------------------------------------------------------------------------
# get_all_sky_delegated — event sync, cache load/save, daily series
# ---------------------------------------------------------------------------


def test_get_all_sky_delegated_raises_if_rpc_url_missing(monkeypatch):
    """If SKY_RPC_URL is unset and w3 not injected, raise RuntimeError."""
    monkeypatch.delenv("SKY_RPC_URL", raising=False)

    with pytest.raises(RuntimeError, match="SKY_RPC_URL"):
        delegation.get_all_sky_delegated(["0x" + "a" * 40], w3=None)


def test_get_all_sky_delegated_uses_injected_w3(tmp_path):
    """If w3 is provided, ignore SKY_RPC_URL."""
    cache_path = tmp_path / "delegation_cache.json"
    contract = "0x" + "c" * 40

    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 22368738
    mock_w3.eth.get_logs.side_effect = [[], []]  # No events

    # Should not raise even though SKY_RPC_URL unset.
    result = delegation.get_all_sky_delegated(
        [contract],
        w3=mock_w3,
        cache_path=cache_path,
    )

    assert isinstance(result, pd.DataFrame)


def test_get_all_sky_delegated_rebuild_resyncs_from_factory_block(monkeypatch, tmp_path):
    """rebuild=True discards cache and resyncs from V3_FACTORY_BLOCK."""
    monkeypatch.setenv("SKY_RPC_URL", "http://localhost:8545")
    cache_path = tmp_path / "delegation_cache.json"

    # Pre-populate cache with stale last_synced_block.
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cache_path.write_text(json.dumps({"last_synced_block": 99999999}))

    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 100000000
    mock_w3.eth.get_logs.side_effect = [[], []]  # No events

    with patch("ad_voting_metrics.sources.delegation._sync_events") as sync_mock:
        sync_mock.return_value = {"events": {}, "last_synced_block": 100000000}
        delegation.get_all_sky_delegated(
            ["0xaaaa"],
            w3=mock_w3,
            cache_path=cache_path,
            rebuild=True,
        )

        sync_mock.assert_called_once()
        assert sync_mock.call_args[1]["rebuild"] is True


# ---------------------------------------------------------------------------
# get_delegate_list_sky — per-day rows + zero-fill
# ---------------------------------------------------------------------------


def _sky_df_indexed(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Return a DataFrame shaped like get_all_sky_delegated's return value.

    rows: (contract, dt_iso, running_total_balance) tuples.
    """
    df = pd.DataFrame(rows, columns=["delegation_contract", "dt", "running_total_balance"])
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    return df.set_index(["delegation_contract", "dt"])


def test_get_delegate_list_sky_returns_one_row_per_delegate_per_day():
    """For a 2-day period with 1 delegate, output covers both days."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _sky_df_indexed([
        ("0xaaa", "2026-04-01", 1000.0),
        ("0xaaa", "2026-04-02", 1500.0),
    ])
    period_2day = _period_stub(date(2026, 4, 1), date(2026, 4, 2))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_2day)

    assert len(result) == 2
    assert list(result.columns) == ["contract", "name", "date", "sky"]


def test_get_delegate_list_sky_fills_missing_days_with_zero():
    """Period has 3 days; cached data only 1 day; days 1 and 3 are zero."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _sky_df_indexed([
        ("0xaaa", "2026-04-02", 1500.0),
        # days 1 and 3 missing
    ])
    period_3day = _period_stub(date(2026, 4, 1), date(2026, 4, 3))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_3day)

    by_date = dict(zip(result["date"], result["sky"], strict=True))
    assert by_date[date(2026, 4, 1)] == 0.0
    assert by_date[date(2026, 4, 2)] == 1500.0
    assert by_date[date(2026, 4, 3)] == 0.0


def test_get_delegate_list_sky_lowercases_name():
    """The name column is the delegate's name lowercased and stripped."""
    df = pd.DataFrame([
        {"Delegate Name": "  Alice  ", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _sky_df_indexed([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_1day)

    assert result.iloc[0]["name"] == "alice"


def test_get_delegate_list_sky_multiple_delegates():
    """Two delegates produce separate (contract, name, sky) entries per day."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _sky_df_indexed([
        ("0xaaa", "2026-04-01", 1000.0),
        ("0xbbb", "2026-04-01", 500.0),
    ])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_1day)

    by_name = dict(zip(result["name"], result["sky"], strict=True))
    assert by_name == {"alice": 1000.0, "bob": 500.0}


# ---------------------------------------------------------------------------
# build_sky_lookup — O(1) dict from DataFrame
# ---------------------------------------------------------------------------


def test_build_sky_lookup_returns_dict_keyed_by_contract_date():
    """Materialize df_sky into (contract, date) -> balance dict."""
    df_sky = pd.DataFrame([
        {"contract": "0xaaa", "date": date(2026, 4, 1), "sky": 1000.0},
        {"contract": "0xbbb", "date": date(2026, 4, 1), "sky": 500.0},
    ])

    result = delegation.build_sky_lookup(df_sky)

    assert result["0xaaa", date(2026, 4, 1)] == 1000.0
    assert result["0xbbb", date(2026, 4, 1)] == 500.0


# ---------------------------------------------------------------------------
# read_sync_state — cache metadata
# ---------------------------------------------------------------------------


def test_read_sync_state_returns_factory_block_and_last_synced(tmp_path):
    """read_sync_state fetches factory_block and last_synced_block from cache."""
    cache_path = tmp_path / "delegation_cache.json"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"last_synced_block": 22500000}))

    result = delegation.read_sync_state(cache_path)

    assert result["factory_block"] == 22368737
    assert result["last_synced_block"] == 22500000


def test_read_sync_state_returns_factory_block_if_cache_empty(tmp_path):
    """If cache doesn't exist or is empty, last_synced_block defaults to factory_block."""
    cache_path = tmp_path / "delegation_cache.json"

    result = delegation.read_sync_state(cache_path)

    assert result["factory_block"] == 22368737
    assert result["last_synced_block"] == 22368737
