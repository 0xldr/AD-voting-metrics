"""Tests for sky_dao — focused on the dune-client integration."""

from unittest.mock import MagicMock, patch

import pytest

from ad_voting_metrics import sky_dao


def test_get_all_sky_delegated_calls_run_query_with_query_id(monkeypatch):
    """get_all_sky_delegated() should call run_query with QueryBase whose
    query_id is DUNE_SKY_QUERY_ID, and return rows from get_rows()."""
    monkeypatch.setenv("DUNE_API_KEY", "fake-key")

    fake_rows = [
        {"delegation-contract": "0xabc", "dt": "2026-03-01", "running-total-balance": 1000},
        {"delegation-contract": "0xdef", "dt": "2026-03-01", "running-total-balance": 2000},
    ]
    fake_results = MagicMock()
    fake_results.get_rows.return_value = fake_rows
    fake_client = MagicMock()
    fake_client.run_query.return_value = fake_results

    with patch("ad_voting_metrics.sky_dao.DuneClient", return_value=fake_client) as mock_class:
        result = sky_dao.get_all_sky_delegated()

    mock_class.assert_called_once_with(api_key="fake-key")
    fake_client.run_query.assert_called_once()
    call_kwargs = fake_client.run_query.call_args.kwargs
    assert "query" in call_kwargs
    assert call_kwargs["query"].query_id == sky_dao.DUNE_SKY_QUERY_ID
    assert result == fake_rows


def test_get_all_sky_delegated_raises_when_api_key_missing(monkeypatch):
    """If DUNE_API_KEY is not set, raise runtimeError with a clear message
    rather than letting dune-client fail with an opaque error from
    inside the SDK."""
    monkeypatch.delenv("DUNE_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="DUNE_API_KEY"):
        sky_dao.get_all_sky_delegated()


def test_dune_query_id_is_6604139():
    """Pin the query ID - bumping it requires a deliberate edit."""
    assert sky_dao.DUNE_SKY_QUERY_ID == 6604139
