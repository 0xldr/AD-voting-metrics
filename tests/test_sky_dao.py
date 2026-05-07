"""Tests for sky_dao — focused on the dune-client integration."""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ad_voting_metrics import sky_dao

# ---------------------------------------------------------------------------
# get_all_sky_delegated — dune-client integration and DataFrame return shape
# ---------------------------------------------------------------------------


def test_get_all_sky_delegated_calls_run_query_dataframe(monkeypatch):
    """get_all_sky_delegated() calls run_query_dataframe with a QueryBase
    whose query_id is DUNE_SKY_QUERY_ID, normalizes the result, and
    returns it indexed on (delegation_contract, dt).
    """
    monkeypatch.setenv("DUNE_API_KEY", "fake-key")

    # Dune returns mixed-case addresses sometimes; verify normalization
    fake_df = pd.DataFrame(
        [
            {"delegation_contract": "0xABC", "dt": "2026-03-01", "running_total_balance": 1000.0},
            {"delegation_contract": "0xdef", "dt": "2026-03-01", "running_total_balance": 2000.0},
        ]
    )
    fake_client = MagicMock()
    fake_client.run_query_dataframe.return_value = fake_df

    with patch("ad_voting_metrics.sky_dao.DuneClient", return_value=fake_client) as mock_class:
        result = sky_dao.get_all_sky_delegated()

    # Client constructed with the api key from env
    mock_class.assert_called_once_with(api_key="fake-key")
    # run_query_dataframe called with QueryBase carrying our query ID
    fake_client.run_query_dataframe.assert_called_once()
    call_kwargs = fake_client.run_query_dataframe.call_args.kwargs
    assert "query" in call_kwargs
    assert call_kwargs["query"].query_id == sky_dao.DUNE_SKY_QUERY_ID

    # Result is indexed on (contract, dt), with contract lowercased
    assert isinstance(result, pd.DataFrame)
    assert result.index.names == ["delegation_contract", "dt"]
    # The 0xABC contract from input was lowercased
    assert ("0xabc", "2026-03-01") in result.index
    assert ("0xdef", "2026-03-01") in result.index


def test_get_all_sky_delegated_uses_cache_when_cache_hours_set(monkeypatch):
    """When cache_max_age_hours is provided, get_latest_resultis called
    instead of run_query_dataframe, with the threshold passed through."""
    monkeypatch.setenv("DUNE_API_KEY", "fake-key")

    fake_results = MagicMock()
    fake_results.get_rows.return_value = [
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1500.0},
    ]
    fake_client = MagicMock()
    fake_client.get_latest_result.return_value = fake_results

    with patch("ad_voting_metrics.sky_dao.DuneClient", return_value=fake_client):
        result = sky_dao.get_all_sky_delegated(cache_max_age_hours=24)

    # Cached path
    fake_client.get_latest_result.assert_called_once()
    call_kwargs = fake_client.get_latest_result.call_args.kwargs
    assert call_kwargs["max_age_hours"] == 24
    assert call_kwargs["query"].query_id == sky_dao.DUNE_SKY_QUERY_ID
    fake_client.run_query_dataframe.assert_not_called()

    assert isinstance(result, pd.DataFrame)
    assert result.index.names == ["delegation_contract", "dt"]
    assert ("0xabc", "2026-03-01") in result.index


def test_get_all_sky_delegated_raises_when_api_key_missing(monkeypatch):
    """If DUNE_API_KEY is not set, raise runtimeError with a clear message."""
    monkeypatch.delenv("DUNE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DUNE_API_KEY"):
        sky_dao.get_all_sky_delegated()


def test_dune_query_id_is_6604139():
    """Pin the query ID - bumping it requires a deliberate edit."""
    assert sky_dao.DUNE_SKY_QUERY_ID == 6604139


# ---------------------------------------------------------------------------
# get_sky_delegated — indexed lookups against the dataframe
# ---------------------------------------------------------------------------


def _make_indexed_df(rows: list[dict]) -> pd.DataFrame:
    """Build a DataFrame in the shape get_all_sky_delegated produces:
    columns delegation_contract (lowercased), dt (string YYYY-MM-DD),
    running_total_balance, indexed on (delegation_contract, dt)."""
    df = pd.DataFrame(rows)
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = df["dt"].astype(str)
    return df.set_index(["delegation_contract", "dt"])


def test_get_sky_delegated_returns_balance_for_known_pair():
    df = _make_indexed_df(
        [{"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5}]
    )
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 3, 1))

    assert result == 1234.5


def test_get_sky_delegated_returns_zero_for_missing_contract():
    df = _make_indexed_df(
        [
            {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5},
        ]
    )

    result = sky_dao.get_sky_delegated(df, "0xnope", date(2026, 3, 1))

    assert result == 0


def test_get_sky_delegated_returns_zero_for_missing_date():
    df = _make_indexed_df(
        [{"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5}]
    )
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 4, 1))

    assert result == 0


def test_get_sky_delegated_normalizes_address_case():
    df = _make_indexed_df(
        [{"delegation_contract": "0xfc48fbca", "dt": "2026-03-01", "running_total_balance": 999.0}]
    )
    result = sky_dao.get_sky_delegated(df, "0xFc48fBcA", date(2026, 3, 1))

    assert result == 999.0


def test_get_sky_delegated_normalizes_address_whitespace():
    df = _make_indexed_df(
        [{"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1000.0}]
    )
    result = sky_dao.get_sky_delegated(df, "  0xabc  ", date(2026, 3, 1))

    assert result == 1000.0


def test_get_sky_delegated_returns_float():
    df = _make_indexed_df(
        [{"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1000.0}]
    )
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 3, 1))

    assert isinstance(result, float)
    assert result == 1000.0


def test_get_sky_delegated_indexed_lookup_handles_many_dates():
    rows = [
        {
            "delegation_contract": f"0x{i:040x}",
            "dt": f"2026-03-{day:02d}",
            "running_total_balance": float(i * 100 + day),
        }
        for i in range(20)
        for day in range(1, 31)
    ]
    df = _make_indexed_df(rows)

    # Pick a specific entry from the middle of the dataset
    target_contract = f"0x{10:040x}"
    result = sky_dao.get_sky_delegated(df, target_contract, date(2026, 3, 15))

    assert result == 1015.0  # i=10, day=15 -> 10 * 100 + 5
