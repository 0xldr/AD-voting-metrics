"""Tests for sources.sky_polling — vote.sky.money polling endpoints."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
import responses

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import http as http_module
from ad_voting_metrics.sources import sky_polling


@pytest.fixture(autouse=True)
def _reset_session():
    """Clear the cached requests.Session before and after every test in this module."""
    http_module.get_session.cache_clear()
    yield
    http_module.get_session.cache_clear()


# ---------------------------------------------------------------------------
# get_vote_poll_ids — per-poll vote status, voter-set boundary normalization
# ---------------------------------------------------------------------------


def _sky_lookup(rows: list[tuple[str, date, float]]) -> dict[tuple[str, date], float]:
    """Return a sky_lookup dict: (contract, date) → balance."""
    return {(contract, day): sky for contract, day, sky in rows}


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
    sky_lookup = _sky_lookup([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
    ])
    poll_info = [
        {"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)},
        {"pollId": 5678, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)},
    ]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_polling.get_vote_poll_ids(poll_info, df, sky_lookup, current_datetime=_CLOSED_POLL_NOW)

    assert "1234" in result.columns
    assert "5678" in result.columns


def test_get_vote_poll_ids_normalizes_voter_address_case():
    """Mixed-case voter addresses from the API are lowercased at the boundary."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    sky_lookup = _sky_lookup([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        # API returns mixed-case voter address; lowercasing at boundary
        # makes the match work against the lowercase df contract.
        mock_session.return_value.get.return_value = _mock_poll_response(["0xAAA"])
        result = sky_polling.get_vote_poll_ids(poll_info, df, sky_lookup, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Yes"


def test_get_vote_poll_ids_not_started_if_poll_ended_before_delegate_start():
    """If poll endDate < delegate's Start Date, status overridden to 'Not Started'."""
    df = pd.DataFrame([
        # Alice's start date is AFTER the poll ends
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2026-05-01"},
    ])
    sky_lookup = _sky_lookup([])  # no SKY data
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        result = sky_polling.get_vote_poll_ids(poll_info, df, sky_lookup, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Not Started"


def test_get_vote_poll_ids_empty_poll_info_leaves_df_unchanged():
    """No polls → df has no new columns added."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
    ])
    sky_lookup = _sky_lookup([])
    original_columns = list(df.columns)

    with patch("ad_voting_metrics.sources.sky_polling.get_session"):
        result = sky_polling.get_vote_poll_ids([], df, sky_lookup, current_datetime=_CLOSED_POLL_NOW)

    assert list(result.columns) == original_columns


def test_get_vote_poll_ids_multiple_delegates_per_poll():
    """Each delegate row gets its own per-poll status."""
    df = pd.DataFrame([
        {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": "2024-01-01"},
    ])
    sky_lookup = _sky_lookup([
        ("0xaaa", date(2026, 4, 1), 1000.0),
        ("0xaaa", date(2026, 4, 2), 1000.0),
        ("0xaaa", date(2026, 4, 3), 1000.0),
        ("0xbbb", date(2026, 4, 1), 500.0),
        ("0xbbb", date(2026, 4, 2), 500.0),
        ("0xbbb", date(2026, 4, 3), 500.0),
    ])
    poll_info = [{"pollId": 1234, "startDate": date(2026, 4, 1), "endDate": date(2026, 4, 3)}]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        # Alice voted, Bob didn't
        mock_session.return_value.get.return_value = _mock_poll_response(["0xaaa"])
        result = sky_polling.get_vote_poll_ids(poll_info, df, sky_lookup, current_datetime=_CLOSED_POLL_NOW)

    assert result.loc[0, "1234"] == "Yes"
    assert result.loc[1, "1234"] == "No"


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
        sky_polling.SKY_ALL_POLLS_URL,
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

    result = sky_polling.get_poll_ids(period)

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
        sky_polling.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 2},
            "polls": [_poll_dict(201, "2025-04-02T00:00:00Z", "2025-04-05T16:00:00Z", "Page-1 poll")],
        },
        status=200,
    )
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 2},
            "polls": [_poll_dict(202, "2025-04-20T00:00:00Z", "2025-04-23T16:00:00Z", "Page-2 poll")],
        },
        status=200,
    )

    result = sky_polling.get_poll_ids(period)

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
        sky_polling.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": [],
            "polls": [_poll_dict(301, "2025-04-02T00:00:00Z", "2025-04-05T16:00:00Z")],
        },
        status=200,
    )

    result = sky_polling.get_poll_ids(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_poll_ids_stops_on_empty_polls_list():
    """Empty polls list terminates the loop without raising."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 5}, "polls": []},
        status=200,
    )

    result = sky_polling.get_poll_ids(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_get_poll_ids_request_url_includes_period_start():
    """The startDate query parameter is the period's first day in ISO form."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 1}, "polls": []},
        status=200,
    )

    sky_polling.get_poll_ids(period)

    url = responses.calls[0].request.url
    assert url is not None
    assert "startDate=2025-04-01" in url
    assert "network=mainnet" in url
    assert f"pageSize={sky_polling.SKY_POLL_PAGE_SIZE}" in url
