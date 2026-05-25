"""Tests for sources.dune — Dune client integration and (delegate, day) projection."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ad_voting_metrics.sources import dune
from ad_voting_metrics.sources import http as http_module


def _period_stub(start: date, end: date) -> SimpleNamespace:
    """Build a period-shaped stub covering [start, end].

    Tests need arbitrary date ranges; real MonthPeriod only models calendar months.
    The dune module only reads .start/.end on the period it's handed.
    """
    return SimpleNamespace(start=start, end=end, year=start.year, month=start.month)


@pytest.fixture(autouse=True)
def _reset_session():
    """Clear the cached requests.Session before and after every test in this module."""
    http_module.get_session.cache_clear()
    yield
    http_module.get_session.cache_clear()


# ---------------------------------------------------------------------------
# get_all_sky_delegated — dune-client integration and DataFrame return shape
# ---------------------------------------------------------------------------


def test_get_all_sky_delegated_calls_run_query_dataframe(monkeypatch):
    """Call run_query_dataframe with DUNE_SKY_QUERY_ID; lowercase contract and index on (contract, dt)."""
    monkeypatch.setenv("DUNE_API_KEY", "fake-key")

    # Dune returns mixed-case addresses sometimes; verify normalization
    fake_df = pd.DataFrame([
        {"delegation_contract": "0xABC", "dt": "2026-03-01", "running_total_balance": 1000.0},
        {"delegation_contract": "0xdef", "dt": "2026-03-01", "running_total_balance": 2000.0},
    ])
    fake_client = MagicMock()
    fake_client.run_query_dataframe.return_value = fake_df

    with patch("ad_voting_metrics.sources.dune.DuneClient", return_value=fake_client) as mock_class:
        result = dune.get_all_sky_delegated()

    # Client constructed with the api key from env
    mock_class.assert_called_once_with(api_key="fake-key")
    # run_query_dataframe called with QueryBase carrying our query ID
    fake_client.run_query_dataframe.assert_called_once()
    call_kwargs = fake_client.run_query_dataframe.call_args.kwargs
    assert "query" in call_kwargs
    assert call_kwargs["query"].query_id == dune.DUNE_SKY_QUERY_ID

    # Result is indexed on (contract, dt), with contract lowercased and dt as date.
    assert isinstance(result, pd.DataFrame)
    assert result.index.names == ["delegation_contract", "dt"]
    # The 0xABC contract from input was lowercased
    assert ("0xabc", date(2026, 3, 1)) in result.index
    assert ("0xdef", date(2026, 3, 1)) in result.index


def test_get_all_sky_delegated_uses_cache_when_cache_hours_set(monkeypatch):
    """Call get_latest_result when cache_max_age_hours is set; pass the threshold through."""
    monkeypatch.setenv("DUNE_API_KEY", "fake-key")

    fake_results = MagicMock()
    fake_results.get_rows.return_value = [
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1500.0},
    ]
    fake_client = MagicMock()
    fake_client.get_latest_result.return_value = fake_results

    with patch("ad_voting_metrics.sources.dune.DuneClient", return_value=fake_client):
        result = dune.get_all_sky_delegated(cache_max_age_hours=24)

    # Cached path
    fake_client.get_latest_result.assert_called_once()
    call_kwargs = fake_client.get_latest_result.call_args.kwargs
    assert call_kwargs["max_age_hours"] == 24
    assert call_kwargs["query"].query_id == dune.DUNE_SKY_QUERY_ID
    fake_client.run_query_dataframe.assert_not_called()

    assert isinstance(result, pd.DataFrame)
    assert result.index.names == ["delegation_contract", "dt"]
    assert ("0xabc", date(2026, 3, 1)) in result.index


def test_get_all_sky_delegated_raises_when_api_key_missing(monkeypatch):
    """If DUNE_API_KEY is not set, raise runtimeError with a clear message."""
    monkeypatch.delenv("DUNE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DUNE_API_KEY"):
        dune.get_all_sky_delegated()


# ---------------------------------------------------------------------------
# get_delegate_list_sky — per-day SKY rows + ranking inputs
# ---------------------------------------------------------------------------


def _all_sky_df(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Return a DataFrame shaped like get_all_sky_delegated's return value.

    rows is a list of (contract, dt_iso, running_total_balance) tuples.
    The returned DataFrame is indexed on (delegation_contract, dt) with
    contract lowercased and dt as datetime.date — matching get_all_sky_delegated.
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
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-01", 1000.0),
        ("0xaaa", "2026-04-02", 1500.0),
    ])
    period_2day = _period_stub(date(2026, 4, 1), date(2026, 4, 2))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_2day)

    assert len(result) == 2
    assert list(result.columns) == ["contract", "name", "date", "sky"]


def test_get_delegate_list_sky_fills_missing_dune_days_with_zero():
    """Period has 3 days; Dune only has data for day 2; days 1 and 3 are zero."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-02", 1500.0),
        # day 1 and day 3 missing
    ])
    period_3day = _period_stub(date(2026, 4, 1), date(2026, 4, 3))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_3day)

    by_date = dict(zip(result["date"], result["sky"], strict=True))
    assert by_date[date(2026, 4, 1)] == 0
    assert by_date[date(2026, 4, 2)] == 1500.0
    assert by_date[date(2026, 4, 3)] == 0


def test_get_delegate_list_sky_lowercases_and_strips_name():
    """The name column is the delegate's name lowercased and stripped."""
    df = pd.DataFrame([
        {"Delegate Name": "  Alice  ", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_1day)

    assert result.iloc[0]["name"] == "alice"


def test_get_delegate_list_sky_preserves_unrounded_sky():
    """The sky column carries the raw Dune value; rounding is the caller's job."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1234.56789)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_1day)

    assert result.iloc[0]["sky"] == 1234.56789


def test_get_delegate_list_sky_date_is_date_object():
    """The date column holds datetime.date objects, not strings."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_1day)

    assert isinstance(result.iloc[0]["date"], date)


def test_get_delegate_list_sky_multiple_delegates_distinct_rows():
    """Two delegates produce separate (contract, name, sky) entries per day."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-01", 1000.0),
        ("0xbbb", "2026-04-01", 500.0),
    ])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky):
        result = dune.get_delegate_list_sky(df, period_1day)

    by_name = dict(zip(result["name"], result["sky"], strict=True))
    assert by_name == {"alice": 1000.0, "bob": 500.0}
    by_contract = dict(zip(result["contract"], result["sky"], strict=True))
    assert by_contract == {"0xaaa": 1000.0, "0xbbb": 500.0}


def test_get_delegate_list_sky_passes_cache_max_age_hours_through():
    """cache_max_age_hours forwards to get_all_sky_delegated."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(dune, "get_all_sky_delegated", return_value=fake_all_sky) as mock_dune:
        dune.get_delegate_list_sky(df, period_1day, cache_max_age_hours=24)

    mock_dune.assert_called_once_with(cache_max_age_hours=24)
