"""Tests for sky_dao — focused on the dune-client integration."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from ad_voting_metrics import sky_dao
from ad_voting_metrics.period import MonthPeriod

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


# ---------------------------------------------------------------------------
# get_delegate_list_sky — characterization tests pinning current behavior
# before the refactor that flattens the four-level dict accumulator.
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


def test_get_delegate_list_sky_rank_field_constant_one():
    """The Rank field is a placeholder constant 1 in every row.

    Ranking is computed downstream; this function just builds the inputs.
    """
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    fake_all_sky = _all_sky_df([
        ("0xaaa", "2026-04-01", 5000.0),
        ("0xbbb", "2026-04-01", 1000.0),
    ])
    period_1day = MagicMock(spec=MonthPeriod)
    period_1day.start = date(2026, 4, 1)
    period_1day.end = date(2026, 4, 1)

    with patch.object(sky_dao, "get_all_sky_delegated", return_value=fake_all_sky):
        _, rank_list = sky_dao.get_delegate_list_sky(df, period_1day)

    assert all(r["Rank"] == 1 for r in rank_list)


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
# custom_sort — characterization tests pinning current behavior before
# the refactor that replaces the lambda + linear lookups with dict maps.
# ---------------------------------------------------------------------------


def _custom_sort_df(rows: list[dict]) -> pd.DataFrame:
    """Return a DataFrame in the shape custom_sort expects.

    Required columns: Delegate Name, Delegate Contract, Start Date, then
    one column per poll/spell with status values.
    """
    return pd.DataFrame(rows)


def test_custom_sort_drops_start_date_and_keeps_status_columns():
    """custom_sort drops the Start Date column and preserves status columns."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[{"pollId": "1234", "title": "Poll T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    # After transpose, columns are the (sorted) delegate rows plus the
    # 3 prepended metadata columns. Start Date column is gone.
    # The index contains the original column names; "Start Date" must not be present.
    assert "Start Date" not in result.index


def test_custom_sort_replaces_delegate_name_with_delegate_column():
    """custom_sort renames "Delegate Name" to "Delegate" (positioned after Contract)."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[{"pollId": "1234", "title": "Poll T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    # In the transposed result, the "Delegate" column became the "Poll Id" row,
    # holding the delegate names (here, "Alice").
    assert "Poll Id" in result.index
    # The first delegate-data column carries "Alice" in the Poll Id row.
    poll_id_row = result.loc["Poll Id"]
    # Skip the 3 metadata columns at the start
    assert "Alice" in list(poll_id_row)


def test_custom_sort_metadata_lookup_for_poll_columns():
    """For columns whose name matches a pollId, populate title/startDate/endDate."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    poll = {"pollId": "1234", "title": "Atlas Edit", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[poll],
        spell_info=[],
    )

    # Find the "1234" row (the poll's column after transpose).
    assert result.loc["1234", "Title"] == "Atlas Edit"
    assert result.loc["1234", "Start Date"] == date(2026, 4, 1)
    assert result.loc["1234", "End Date"] == date(2026, 4, 3)


def test_custom_sort_metadata_lookup_for_spell_columns():
    """For columns matching a spell address, populate title/startDate; End Date is "N/A"."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "0xspell001": "Yes"},
    ])
    spell = {"address": "0xspell001", "title": "Spell T", "startDate": date(2026, 4, 5)}
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[],
        spell_info=[spell],
    )

    assert result.loc["0xspell001", "Title"] == "Spell T"
    assert result.loc["0xspell001", "Start Date"] == date(2026, 4, 5)
    assert result.loc["0xspell001", "End Date"] == "N/A"


def test_custom_sort_unknown_column_gets_placeholder_metadata():
    """Columns not matching any poll/spell get placeholder strings.

    These rows are the original metadata columns ("Delegate Contract",
    "Delegate"); the placeholders fill what would otherwise be NaN.
    """
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[{"pollId": "1234", "title": "T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    # The "" row (former Delegate Contract column) gets placeholder metadata
    assert result.loc["", "Title"] == "Title"
    assert result.loc["", "Start Date"] == "Start Date"
    assert result.loc["", "End Date"] == "End Date"


def test_custom_sort_adds_blank_rows_for_missing_hardcoded_order_addresses():
    """Addresses in hardcoded_order but missing from df get blank rows added."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    # hardcoded_order has 0xaaa AND 0xmissing; the latter isn't in df
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa", "0xmissing"],
        poll_info=[{"pollId": "1234", "title": "T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    # After transpose, the "" row carries the Delegate Contract values
    # spread across columns. Both addresses should appear.
    empty_row = result.loc[""]
    values = list(empty_row)
    assert "0xaaa" in values
    assert "0xmissing" in values


def test_custom_sort_orders_delegates_by_hardcoded_order():
    """Rows are sorted by hardcoded_order's position; unknowns go to the end."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "A"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01", "1234": "B"},
        {"Delegate Name": "Carol", "Delegate Contract": "0xccc", "Start Date": "2024-01-01", "1234": "C"},
    ])
    # Reverse the natural order of bbb/aaa/ccc in hardcoded_order
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xbbb", "0xaaa", "0xccc"],
        poll_info=[{"pollId": "1234", "title": "T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    # The poll row "1234" carries the per-delegate vote statuses;
    # ordering follows hardcoded_order.
    poll_row = result.loc["1234"]
    # Skip the 3 metadata columns (Start Date, End Date, Title)
    statuses = [v for v in poll_row if v in {"A", "B", "C"}]
    assert statuses == ["B", "A", "C"]


def test_custom_sort_transpose_first_three_columns_are_metadata():
    """After insert, columns 0/1/2 are Start Date, End Date, Title."""
    df = _custom_sort_df([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01", "1234": "Yes"},
    ])
    result = sky_dao.custom_sort(
        df,
        hardcoded_order=["0xaaa"],
        poll_info=[{"pollId": "1234", "title": "T", "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}],
        spell_info=[],
    )

    assert list(result.columns[:3]) == ["Start Date", "End Date", "Title"]


# ---------------------------------------------------------------------------
# get_vote_poll_ids — characterization tests pinning current behavior before
# the refactor that replaces per-(poll, delegate) df_sky filtering with a
# precomputed dict lookup.
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


def test_get_vote_poll_ids_voted_returns_yes():
    """Delegate appearing in votesByAddress gets status 'Yes'."""
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
        mock_session.return_value.get.return_value = _mock_poll_response(["0xaaa"])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Yes"


def test_get_vote_poll_ids_not_voted_with_sky_returns_no():
    """Closed poll, didn't vote, had SKY throughout → 'No'."""
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
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "No"


def test_get_vote_poll_ids_poll_still_open_returns_voting_open():
    """Poll still open + delegate didn't vote yet → 'Voting Open'."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]
    # Before poll closes (2026-04-03 16:00 UTC)
    open_now = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=open_now)

    assert result.loc[0, "1234"] == "Voting Open"


def test_get_vote_poll_ids_no_delegated_sky_returns_no_delegated_sky():
    """No SKY anywhere in window → 'No Delegated SKY'."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([
        ("0xaaa", date(2026, 4, 1), 0.0),
        ("0xaaa", date(2026, 4, 2), 0.0),
        ("0xaaa", date(2026, 4, 3), 0.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "No Delegated SKY"


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


def test_get_vote_poll_ids_mutates_df_in_place():
    """Documented contract: the same df object is returned, mutated in place."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 1), 1000.0)])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_dao.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=_CLOSED_POLL_NOW)

    assert result is df


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
# get_vote_executive_ids — characterization tests
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


def test_get_vote_executive_ids_mutates_df_in_place():
    """The same df object is returned, mutated in place."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    df_sky = _df_sky_for_window([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sky_dao.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_executive_supporters_response({})
        result = sky_dao.get_vote_executive_ids(spell_info, df, df_sky)

    assert result is df


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
