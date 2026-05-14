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
    """Call run_query_dataframe with the SKY query and return a normalized, indexed DataFrame.

    Verifies query_id is DUNE_SKY_QUERY_ID, the result is lowercased
    and indexed on (delegation_contract, dt)
    """
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
    """Call get_latest_result instead of run_query_dataframe when cache_max_age_hours is set.

    Pass the threshold through the cache call.
    """
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
    """Treat a delegate with no Dune rows in the window as all-zero.

    Defensive: an earlier code path could silently fall through to a
    stale bool value.
    """
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
    """Scenario C: zero on days 0-2, non-zero only on close day, didn't vote.

    Grace period covers this — a delegation arriving same-day as close
    can't reasonably be acted on.
    """
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_grace_period_close_day_only_voted_returns_yes():
    """Scenario D: same as C but they voted anyway.

    They engaged despite the late delegation - count it as Yes.
    """
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_AFTER_CLOSE
        )
        == "Yes"
    )


def test_status_pre_close_day_delegation_still_returns_no():
    """Non-zero on day 2 and day 3, didn't vote, return No.

    Both conditions for No are met: SKY at close and SKY beforehand.
    The delegate had time and stake - standard non-participation.
    """
    sky = _sky_dict(0, 0, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No"
    )


def test_status_mid_window_only_returns_no_delegated_sky():
    """Non-zero only mid-window, zero on close day, didn't vote -> No Delegated SKY.

    The delegate had no SKY at close, so even if they'd wanted to vote
    on day 1 (and didn't), they couldn't have completed it at close.
    """
    sky = _sky_dict(0, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_voted_but_withdrew_before_close_returns_yes():
    """A recorded vote stands even if the delegations were pulled before close.

    Close-day-zero doesn't override a recorded vote.
    """
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_AFTER_CLOSE
        )
        == "Yes"
    )


def test_status_grief_vector_blocked():
    """A hostile delegator dropping 1 SKY on close day cannot harm the target.

    Without grace: marked No, participation % drops, eligibility may
    flip. With grace: marked No Delegated SKY, discounted, target
    unaffected.
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

    Under the close-day rule the delegat had zero stake at the
    decisive moment, so they're discounted rather than penalized.
    Voting requires SKY at close to even be possible.
    """
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_date_uses_what_is_present():
    """Treat missing days as zero when the sky dict is sparse.

    Only day 3 has a row, and it's non-zero. Grace applies.
    """
    sky = {date(2026, 4, 4): 1000.0}
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_AFTER_CLOSE
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_pre_close_only_returns_no_delegated_sky():
    """Sky on day 1 only (no close-day row -> close-day treated as zero) -> No Delegated SKY.

    Under the close-day rule the delegate had no stake at the decisive
    moment, even though they had some earlier.
    """
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
    """Return 'Voting Open' for a still-open poll the delegate hasn't voted on.

    NOT penalized - they might still vote. 'Voting Open' goes in
    DISCOUNTED so it doesn't affect participation %.
    """
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_DURING_VOTING
        )
        == "Voting Open"
    )


def test_status_in_progress_with_no_sky_still_returns_voting_open():
    """Return 'Voting Open' for an open poll even when the delegate has no SKY now.

    They could still receive a delegation and vote. 'Voting Open' trumps
    'No Delegated Sky' while the poll is still open.
    """
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=_DURING_VOTING
        )
        == "Voting Open"
    )


def test_status_in_progress_voted_with_no_sky_returns_yes():
    """A recorded vote gets credit even with zero SKY at run time.

    The voted flag from the API is authoritative - stake context only
    resolves the non-vote case, never overrules a recorded vote.
    """
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=True, current_datetime=_DURING_VOTING
        )
        == "Yes"
    )


def test_status_close_day_before_1600_utc_treated_as_open():
    """Boundary 1: 15:00 UTC on close day -> still open.

    Poll closes at 16:00 UTC. Full SKY but no vote -> 'Voting Open'.
    """
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "Voting Open"
    )


def test_status_close_day_at_exactly_1600_utc_treated_as_closed():
    """Boundary 2: exactly 16:00 UTC on close day -> closed.

    We treat >= 16:00 UTC as closed, so the close-day rule applies.
    SKY before and at close + no vote -> 'No'.
    """
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "No"
    )


def test_status_close_day_after_1600_utc_treated_as_closed():
    """Boundary 3: 17:00 UTC on close day -> closed.

    Same outcome as exactly-at-close — close-day rule applies.
    """
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 17, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky, _POLL_CLOSE, delegate_voted=False, current_datetime=current
        )
        == "No"
    )
