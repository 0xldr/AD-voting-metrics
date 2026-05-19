"""Tests for sky_dao — focused on the dune-client integration."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import responses

from ad_voting_metrics import sky_dao
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import http as http_module


@pytest.fixture(autouse=True)
def _reset_session():
    """Clear the cached requests.Session before and after every test in this module.

    Auto-applied: the requests adapter caches connections, and the `responses`
    library swaps the adapter in `@responses.activate`. A stale Session can
    bind a test to the previous run's mock state.
    """
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
    """Return a DataFrame in the shape get_all_sky_delegated produces.

    Columns delegation_contract (lowercased), dt (string YYYY-MM-DD),
    running_total_balance, indexed on (delegation_contract, dt).
    """
    df = pd.DataFrame(rows)
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = df["dt"].astype(str)
    return df.set_index(["delegation_contract", "dt"])


def test_get_sky_delegated_returns_balance_for_known_pair():
    df = _make_indexed_df([
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5},
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
        {"delegation_contract": "0xabc", "dt": "2026-03-01", "running_total_balance": 1234.5},
    ])
    result = sky_dao.get_sky_delegated(df, "0xabc", date(2026, 4, 1))

    assert result == 0


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
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_empty_sky_dict_returns_no_delegated_sky():
    """Empty sky dict treated as all-zero rather than falling through to a stale value."""
    assert (
        sky_dao.determine_vote_status(
            {},
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_voted_with_sky_returns_yes():
    """Delegate had SKY and voted."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
        )
        == "Yes"
    )


def test_status_did_not_vote_full_window_returns_no():
    """Delegate has SKY throughout and did not vote."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No"
    )


def test_status_grace_period_close_day_only_no_vote_returns_no_delegated_sky():
    """Scenario C: zero days 0-2, non-zero only on close day, didn't vote → grace applies."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_grace_period_close_day_only_voted_returns_yes():
    """Scenario D: same as C but they voted anyway — counts as Yes."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
        )
        == "Yes"
    )


def test_status_pre_close_day_delegation_still_returns_no():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 0, 1000, 1000)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No"
    )


def test_status_mid_window_only_returns_no_delegated_sky():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_voted_but_withdrew_before_close_returns_yes():
    """A recorded vote stands even if delegations were pulled before close."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
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
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
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
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_date_uses_what_is_present():
    """Missing days are treated as zero; only day 3 has a row → grace applies."""
    sky = {date(2026, 4, 4): 1000.0}
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_pre_close_only_returns_no_delegated_sky():
    """Day-1 only (close day missing → treated as zero) → No Delegated SKY."""
    sky = {date(2026, 4, 2): 1000.0}
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
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
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_DURING_VOTING,
        )
        == "Yes"
    )


def test_status_in_progress_poll_not_voted_returns_voting_open():
    """Open poll, no vote → 'Voting Open' (discounted, not penalised)."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_DURING_VOTING,
        )
        == "Voting Open"
    )


def test_status_in_progress_with_no_sky_still_returns_voting_open():
    """Open poll with no SKY now → still 'Voting Open' (they could still receive a delegation)."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_DURING_VOTING,
        )
        == "Voting Open"
    )


def test_status_in_progress_voted_with_no_sky_returns_yes():
    """Recorded vote stands even with zero SKY at run time — the API's voted flag is authoritative."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_DURING_VOTING,
        )
        == "Yes"
    )


def test_status_close_day_before_1600_utc_treated_as_open():
    """15:00 UTC on close day → still open (close is 16:00 UTC)."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "Voting Open"
    )


def test_status_close_day_at_exactly_1600_utc_treated_as_closed():
    """Exactly 16:00 UTC counts as closed; close-day rule applies."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "No"
    )


def test_status_close_day_after_1600_utc_treated_as_closed():
    """17:00 UTC on close day → closed."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 17, 0, tzinfo=UTC)
    assert (
        sky_dao.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "No"
    )


# ---------------------------------------------------------------------------
# get_delegate_list_sky — per-day SKY rows + ranking inputs
# ---------------------------------------------------------------------------


def _all_sky_df(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Return a DataFrame shaped like get_all_sky_delegated's return value.

    rows is a list of (contract, dt_iso, running_total_balance) tuples.
    The returned DataFrame is indexed on (delegation_contract, dt) with
    contract lowercased — matching get_all_sky_delegated.
    """
    df = pd.DataFrame(rows, columns=["delegation_contract", "dt", "running_total_balance"])
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = df["dt"].astype(str)
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
    period_2day = MagicMock(spec=MonthPeriod)
    period_2day.start = date(2026, 4, 1)
    period_2day.end = date(2026, 4, 2)
    period_2day.year = 2026
    period_2day.month = 4

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        sky_list, rank_list = sky_dao.get_delegate_list_sky(df, period_2day)

    # Two rows each: one per (entity, day).
    assert len(sky_list) == 2
    assert len(rank_list) == 2


def test_get_delegate_list_sky_fills_missing_dune_days_with_zero():
    """Period has 3 days; Dune only has data for day 2; days 1 and 3 are zero."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-02", 1500.0),
        # day 1 and day 3 missing
    ])
    period_3day = MagicMock(spec=MonthPeriod)
    period_3day.start = date(2026, 4, 1)
    period_3day.end = date(2026, 4, 3)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        sky_list, _rank_list = sky_dao.get_delegate_list_sky(df, period_3day)

    by_date = {r["date"]: r["sky"] for r in sky_list}
    assert by_date[date(2026, 4, 1)] == 0
    assert by_date[date(2026, 4, 2)] == 1500.0
    assert by_date[date(2026, 4, 3)] == 0


def test_get_delegate_list_sky_rank_list_lowercases_name():
    """Names in the rank list are lowercased and stripped."""
    df = pd.DataFrame([
        {"Delegate Name": "  Alice  ", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        _, rank_list = sky_dao.get_delegate_list_sky(df, period_1day)

    assert rank_list[0]["Delegate"] == "alice"


def test_get_delegate_list_sky_rank_total_rounded_to_2dp():
    """Total Delegation rounds to 2 decimal places; raw sky is unrounded."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1234.56789)])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        sky_list, rank_list = sky_dao.get_delegate_list_sky(df, period_1day)

    assert rank_list[0]["Total Delegation"] == 1234.57
    # Raw sky stays unrounded
    assert sky_list[0]["sky"] == 1234.56789


def test_get_delegate_list_sky_date_is_date_object():
    """Date fields in both output lists are datetime.date objects, not strings."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        sky_list, rank_list = sky_dao.get_delegate_list_sky(df, period_1day)

    assert isinstance(rank_list[0]["Date"], date)
    assert isinstance(sky_list[0]["date"], date)


def test_get_delegate_list_sky_multiple_delegates_distinct_names():
    """Two distinct delegates produce separate (name, date) and (contract, date) entries."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-01", 1000.0),
        ("0xbbb", "2026-04-01", 500.0),
    ])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        sky_list, rank_list = sky_dao.get_delegate_list_sky(df, period_1day)

    rank_by_name = {r["Delegate"]: r["Total Delegation"] for r in rank_list}
    assert rank_by_name == {"alice": 1000.0, "bob": 500.0}
    sky_by_contract = {r["contract"]: r["sky"] for r in sky_list}
    assert sky_by_contract == {"0xaaa": 1000.0, "0xbbb": 500.0}


def test_get_delegate_list_sky_passes_cache_max_age_hours_through():
    """cache_max_age_hours forwards to get_all_sky_delegated."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky) as mock_dune:
        sky_dao.get_delegate_list_sky(df, period_1day, cache_max_age_hours=24)

    mock_dune.assert_called_once_with(cache_max_age_hours=24)


# ---------------------------------------------------------------------------
# get_vote_poll_ids — per-poll vote status, voter-set boundary normalization
# ---------------------------------------------------------------------------


def _df_sky_for_window(rows: list[tuple[str, date, float]]) -> pd.DataFrame:
    """Return a df_sky-shaped DataFrame: columns contract, date, sky."""
    return pd.DataFrame(rows, columns=["contract", "date", "sky"])


def _mock_poll_response(voter_addresses: list[str]) -> MagicMock:
    """Return a mock for the polls/tally/{pollId} endpoint."""
    response = MagicMock()
    response.json.return_value = {"votesByAddress": [{"voter": addr} for addr in voter_addresses]}
    response.raise_for_status.return_value = None
    return response


_CLOSED_POLL_NOW = datetime(2026, 4, 10, 17, 0, tzinfo=UTC)  # after poll ends 2026-04-03 16:00


def test_get_vote_poll_ids_adds_column_per_poll():
    """Each poll in poll_info gets its own column on df, keyed by str(pollId)."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
    ])
    poll_info = [
        {"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)},
        {"pollId": 5678, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)},
    ]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert "1234" in result.columns
    assert "5678" in result.columns


def test_get_vote_poll_ids_normalizes_voter_address_case():
    """Mixed-case voter addresses from the API are lowercased at the boundary."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        # API returns mixed-case voter address; lowercasing at boundary
        # makes the match work against the lowercase df contract.
        mock_session.return_value.get.return_value = _mock_poll_response(["0xAAA"])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Yes"


def test_get_vote_poll_ids_not_started_if_poll_ended_before_delegate_start():
    """If poll endDate < delegate's Start Date, status overridden to 'Not Started'."""
    df = pd.DataFrame([
        # Alice's start date is AFTER the poll ends
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2026-05-01"},
    ])
    df_sky = _df_sky_for_window([])  # no SKY data
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Not Started"


def test_get_vote_poll_ids_empty_poll_info_leaves_df_unchanged():
    """No polls → df has no new columns added."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([])
    original_columns = list(df.columns)

    with patch("ad_voting_metrics.sky_dao.get_session"):
        result = sky_dao.get_vote_poll_ids([], df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert list(result.columns) == original_columns


def test_get_vote_poll_ids_multiple_delegates_per_poll():
    """Each delegate row gets its own per-poll status."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
        ("0xbbb", date(2026, 4, 1), 500.0),
        ("0xbbb", date(2026, 4, 2), 500.0),
        ("0xbbb", date(2026, 4, 3), 500.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        # Alice voted, Bob didn't
        mock_session.return_value.get.return_value = _mock_poll_response(["0xaaa"])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Yes"
    assert result.loc[1, "1234"] == "No"


# ---------------------------------------------------------------------------
# get_vote_executive_ids — per-spell vote status, supporter-set normalization
# ---------------------------------------------------------------------------


def _mock_executive_supporters_response(supporters_by_spell: dict[str, list[str]]) -> MagicMock:
    """Return a mock for the executive/supporters endpoint.

    supporters_by_spell maps spell_address -> list of supporter addresses.
    """
    response = MagicMock()
    response.json.return_value = {
        spell_addr: [{"address": s} for s in supporters] for spell_addr, supporters in supporters_by_spell.items()
    }
    response.raise_for_status.return_value = None
    return response


def test_get_vote_executive_ids_adds_column_per_spell():
    """Each spell in spell_info gets its own column on df, keyed by spell address."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [
        {"address": "0xspell1", "startDate": date(2026, 4, 5)},
        {"address": "0xspell2", "startDate": date(2026, 4, 5)},
    ]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response({})
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert "0xspell1" in result.columns
    assert "0xspell2" in result.columns


def test_get_vote_executive_ids_supporter_with_sky_returns_yes():
    """Delegate in supporters + non-zero SKY on startDate → 'Yes'."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response(
            {"0xspell1": ["0xaaa"]},
        )
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert result.loc[0, "0xspell1"] == "Yes"


def test_get_vote_executive_ids_not_supporter_with_sky_returns_pending():
    """Delegate NOT in supporters + non-zero SKY on startDate → 'Pending verification'."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response(
            {"0xspell1": []},  # no supporters
        )
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert result.loc[0, "0xspell1"] == "Pending verification"


def test_get_vote_executive_ids_zero_sky_returns_no_delegated_sky():
    """SKY balance is 0 on startDate → 'No Delegated SKY'."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 0.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response(
            {"0xspell1": ["0xaaa"]},  # delegate IS a supporter, but no SKY
        )
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert result.loc[0, "0xspell1"] == "No Delegated SKY"


def test_get_vote_executive_ids_not_started_if_spell_started_before_delegate():
    """spell startDate < delegate Start Date → 'Not Started' (overrides everything)."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2026-05-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response(
            {"0xspell1": ["0xaaa"]},
        )
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert result.loc[0, "0xspell1"] == "Not Started"


def test_get_vote_executive_ids_normalizes_supporter_address_case():
    """Mixed-case supporter addresses from the API are lowercased at the boundary.

    The API may return supporter addresses in any case (the spells list
    endpoint returns mixed case; the supporters endpoint's casing isn't
    contractually specified). Lowercasing at the boundary lets downstream
    comparison against pydantic-lowercased contract addresses just work.
    """
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response(
            {"0xspell1": ["0xAAA"]},  # API returns mixed-case
        )
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    # Despite API casing mismatch, the boundary-lowercase makes this work.
    assert result.loc[0, "0xspell1"] == "Yes"


def test_get_vote_executive_ids_empty_spell_info_leaves_df_unchanged():
    """No spells → df has no new columns added (but the HTTP call still happens)."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([])
    original_columns = list(df.columns)

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response({})
        result = sky_dao.get_vote_executive_ids([], df, df_sky)

    assert list(result.columns) == original_columns


# ---------------------------------------------------------------------------
# get_poll_ids — pagination and date filtering against vote.sky.money
# ---------------------------------------------------------------------------


def _poll_dict(poll_id: int, start_iso: str, end_iso: str, title: str = "Test poll") -> dict:
    """Return an API-shaped poll dict with ISO startDate/endDate strings."""
    return {
        "pollId": poll_id,
        "startDate": start_iso,
        "endDate": end_iso,
        "title": title,
    }


@responses.activate
def test_get_poll_ids_single_page_filters_to_period():
    """Polls outside the period are filtered; in-period polls have date-typed fields."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 1},
            "polls": [
                _poll_dict(101, "2025-04-05T00:00:00Z", "2025-04-08T16:00:00Z", "In window"),
                _poll_dict(102, "2025-03-30T00:00:00Z", "2025-04-02T16:00:00Z", "Before window"),
                _poll_dict(103, "2025-05-02T00:00:00Z", "2025-05-05T16:00:00Z", "After window"),
            ],
        },
        status=200,
    )

    result = sky_dao.get_poll_ids(period)

    assert len(result) == 1
    poll = result[0]
    assert poll["pollId"] == 101
    assert poll["title"] == "In window"
    assert poll["startDate"] == date(2025, 4, 5)
    assert poll["endDate"] == date(2025, 4, 8)


@responses.activate
def test_get_poll_ids_paginates_until_numpages_reached():
    """Loop advances `page` until paginationInfo.numPages equals the current page."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 2},
            "polls": [_poll_dict(201, "2025-04-02T00:00:00Z", "2025-04-05T16:00:00Z", "Page-1 poll")],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 2},
            "polls": [_poll_dict(202, "2025-04-20T00:00:00Z", "2025-04-23T16:00:00Z", "Page-2 poll")],
        },
        status=200,
    )

    result = sky_dao.get_poll_ids(period)

    assert [p["pollId"] for p in result] == [201, 202]
    assert len(responses.calls) == 2
    url_1, url_2 = responses.calls[0].request.url, responses.calls[1].request.url
    assert url_1 is not None
    assert url_2 is not None
    assert "page=1" in url_1
    assert "page=2" in url_2


@responses.activate
def test_get_poll_ids_stops_on_empty_pagination_info():
    """Empty paginationInfo terminates the loop without raising."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": [],
            "polls": [_poll_dict(301, "2025-04-02T00:00:00Z", "2025-04-05T16:00:00Z")],
        },
        status=200,
    )

    result = sky_dao.get_poll_ids(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_poll_ids_stops_on_empty_polls_list():
    """Empty polls list terminates the loop without raising."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 5}, "polls": []},
        status=200,
    )

    result = sky_dao.get_poll_ids(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_poll_ids_request_url_includes_period_start():
    """The startDate query parameter is the period's first day in ISO form."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 1}, "polls": []},
        status=200,
    )

    sky_dao.get_poll_ids(period)

    url = responses.calls[0].request.url
    assert url is not None
    assert "startDate=2025-04-01" in url
    assert "network=mainnet" in url
    assert f"pageSize={sky_dao.SKY_POLL_PAGE_SIZE}" in url


# ---------------------------------------------------------------------------
# get_executive_ids — pagination and date filtering against vote.sky.money
# ---------------------------------------------------------------------------


def _executive_dict(address: str, date_iso: str, title: str = "Test spell") -> dict:
    """Return an API-shaped executive dict."""
    return {"address": address, "date": date_iso, "title": title}


@responses.activate
def test_get_executive_ids_filters_to_period_and_lowercases_address():
    """Spells outside the period are dropped; address is lowercased; date typed."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_EXECUTIVE_URL,
        json=[
            _executive_dict("0xAAAA000000000000000000000000000000000001", "2025-04-10T00:00:00Z", "In window"),
            _executive_dict("0xBBBB000000000000000000000000000000000002", "2025-02-10T00:00:00Z", "Before"),
            _executive_dict("0xCCCC000000000000000000000000000000000003", "2025-06-10T00:00:00Z", "After"),
        ],
        status=200,
    )
    # Second page empty → terminates loop.
    responses.add(responses.GET, sky_dao.SKY_EXECUTIVE_URL, json=[], status=200)

    result = sky_dao.get_executive_ids(period)

    assert len(result) == 1
    spell = result[0]
    assert spell["address"] == "0xaaaa000000000000000000000000000000000001"
    assert spell["startDate"] == date(2025, 4, 10)
    assert spell["title"] == "In window"


@responses.activate
def test_get_executive_ids_advances_start_until_empty():
    """The `start` query advances by SKY_EXECUTIVES_PAGE_SIZE until the API returns []."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_dao.SKY_EXECUTIVE_URL,
        json=[_executive_dict("0xspell0000000000000000000000000000000001", "2025-04-05T00:00:00Z")],
        status=200,
    )
    responses.add(
        responses.GET,
        sky_dao.SKY_EXECUTIVE_URL,
        json=[_executive_dict("0xspell0000000000000000000000000000000002", "2025-04-22T00:00:00Z")],
        status=200,
    )
    responses.add(responses.GET, sky_dao.SKY_EXECUTIVE_URL, json=[], status=200)

    result = sky_dao.get_executive_ids(period)

    assert len(result) == 2
    assert len(responses.calls) == 3
    page_size = sky_dao.SKY_EXECUTIVES_PAGE_SIZE
    url_0, url_1, url_2 = (c.request.url for c in responses.calls)
    assert url_0 is not None
    assert url_1 is not None
    assert url_2 is not None
    assert "start=0" in url_0
    assert f"start={page_size}" in url_1
    assert f"start={page_size * 2}" in url_2


@responses.activate
def test_get_executive_ids_empty_first_page_returns_empty():
    """An empty first-page response terminates immediately and returns []."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(responses.GET, sky_dao.SKY_EXECUTIVE_URL, json=[], status=200)

    result = sky_dao.get_executive_ids(period)

    assert result == []
    assert len(responses.calls) == 1
