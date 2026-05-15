"""Tests for sky_dao — focused on the dune-client integration."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import ad_voting_metrics.sky_dao as sky_dao

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
    """Call get_latest_result when cache_max_age_hours is set; pass the threshold through."""
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
    """Build a DataFrame in the shape get_all_sky_delegated produces.

    Columns delegation_contract (lowercased), dt (string YYYY-MM-DD),
    running_total_balance, indexed on (delegation_contract, dt).
    """
    df = pd.DataFrame(rows)
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = df["dt"].astype(str)
    return df.set_index(["delegation_contract", "dt"])


def test_get_sky_delegated_returns_balance_for_known_pair():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5}
    ])
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 3, 1))

    assert result == 1234.5


def test_get_sky_delegated_returns_zero_for_missing_contract():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5},
    ])

    result = sky_dao.get_sky_delegated(df, "0xnope", date(2026, 3, 1))

    assert result == 0


def test_get_sky_delegated_returns_zero_for_missing_date():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5}
    ])
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 4, 1))

    assert result == 0


def test_get_sky_delegated_normalizes_address_case():
    df = _make_indexed_df([
        {"delegation_contract": "0xfc48fbca", "dt": "2026-03-01", "running_total_balance": 999.0}
    ])
    result = sky_dao.get_sky_delegated(df, "0xFc48fBcA", date(2026, 3, 1))

    assert result == 999.0


def test_get_sky_delegated_normalizes_address_whitespace():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1000.0}
    ])
    result = sky_dao.get_sky_delegated(df, "  0xabc  ", date(2026, 3, 1))

    assert result == 1000.0


def test_get_sky_delegated_returns_float():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1000.0}
    ])
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


# ---------------------------------------------------------------------------
# determine_vote_status — voting-status logic with close-day grace period
# ---------------------------------------------------------------------------


# Standard 3-day poll spanning 4 calendar days in Dune's daily rollups:
# 16:00-24:00 on day 0, full days 1 and 2, 0:00-16:00 on day 3 (close day).
_POLL_DAYS = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3), date(2026, 4, 4)]
_POLL_CLOSE = date(2026, 4, 4)
_AFTER_CLOSE = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)  # a date well after poll close
_DURING_VOTING = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)  # a date while poll is still open


def _sky_dict(day0: float, day1: float, day2: float, day3: float) -> dict:
    return dict(zip(_POLL_DAYS, [day0, day1, day2, day3], strict=True))


def test_status_no_sky_anywhere_returns_no_delegated_sky():
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_empty_sky_dict_returns_no_delegated_sky():
    """Empty sky dict treated as all-zero rather than falling through to a stale value."""
    assert (
        sky_dao.determine_vote_status(
            {}, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_voted_with_sky_returns_yes():
    """Delegate had SKY and voted."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_AFTER_CLOSE
        )
        == "Yes"
    )


def test_status_did_not_vote_full_window_returns_no():
    """Delegate has SKY throughout and did not vote."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No"
    )


def test_status_grace_period_close_day_only_no_vote_returns_no_delegated_sky():
    """Scenario C: zero days 0-2, non-zero only on close day, didn't vote → grace applies."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_grace_period_close_day_only_voted_returns_yes():
    """Scenario D: same as C but they voted anyway — counts as Yes."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_AFTER_CLOSE
        )
        == "Yes"
    )


def test_status_pre_close_day_delegation_still_returns_no():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 0, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No"
    )


def test_status_mid_window_only_returns_no_delegated_sky():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_voted_but_withdrew_before_close_returns_yes():
    """A recorded vote stands even if delegations were pulled before close."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_AFTER_CLOSE
        )
        == "Yes"
    )


def test_status_grief_vector_blocked():
    """A hostile delegator dropping 1 SKY on close day cannot harm the target.

    Without grace rule: target marked No, participation % drops,
    eligibility may flip. With it: marked No Delegated SKY, discounted,
    target unaffected.
    """
    sky = _sky_dict(0, 0, 0, 1)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_mid_window_withdrawal_returns_no_delegated_sky():
    """Withdrawn-before-close + no vote → No Delegated SKY, not No.

    Zero stake at the decisive moment → discounted, not penalised.
    """
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_date_uses_what_is_present():
    """Missing days are treated as zero; only day 3 has a row → grace applies."""
    sky = {date(2026, 4, 4): 1000.0}
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_pre_close_only_returns_no_delegated_sky():
    """Day-1 only (close day missing → treated as zero) → No Delegated SKY."""
    sky = {date(2026, 4, 2): 1000.0}
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


# ---------------------------------------------------------------------------
# determine_vote_status — in-progress polls (current_datetime < poll_end_date)
# ---------------------------------------------------------------------------


def test_status_in_progress_poll_voted_returns_yes():
    """Delegate already voted on a poll still open. Counted positively."""
    sky = _sky_dict(1000, 1000, 0, 0)  # data through day 1 only
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_DURING_VOTING
        )
        == "Yes"
    )


def test_status_in_progress_poll_not_voted_returns_voting_open():
    """Open poll, no vote → 'Voting Open' (discounted, not penalised)."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_DURING_VOTING
        )
        == "Voting Open"
    )


def test_status_in_progress_with_no_sky_still_returns_voting_open():
    """Open poll with no SKY now → still 'Voting Open' (they could still receive a delegation)."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_DURING_VOTING
        )
        == "Voting Open"
    )


def test_status_in_progress_voted_with_no_sky_returns_yes():
    """Recorded vote stands even with zero SKY at run time — the API's voted flag is authoritative."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_DURING_VOTING
        )
        == "Yes"
    )


def test_status_close_day_before_1600_utc_treated_as_open():
    """15:00 UTC on close day → still open (close is 16:00 UTC)."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "Voting Open"
    )


def test_status_close_day_at_exactly_1600_utc_treated_as_closed():
    """Exactly 16:00 UTC counts as closed; close-day rule applies."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "No"
    )


def test_status_close_day_after_1600_utc_treated_as_closed():
    """17:00 UTC on close day → closed."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 17, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "No"
    )
