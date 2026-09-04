"""Tests for sources.sky_polling — vote.sky.money polling endpoints."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock, patch

import responses

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.sources import sky_polling

_ADDR_A = "0x" + "a" * 40
_ADDR_B = "0x" + "b" * 40


def _delegate(name: str, address: str, start: date = date(2024, 1, 1)) -> Delegate:
    return Delegate(name=name, vote_delegate_address=address, start_date=start)


def _poll(poll_id: str, start: date, end: date) -> Ballot:
    return Ballot(id=poll_id, kind="poll", start=start, end=end, title="Test poll")


def _sky_lookup(rows: list[tuple[str, date, float]]) -> dict[tuple[str, date], float]:
    """Return a sky_lookup dict: (contract, date) → balance."""
    return {(contract, day): sky for contract, day, sky in rows}


def _sky_throughout(contract: str, sky: float) -> dict[tuple[str, date], float]:
    """Constant balance across the standard 1-3 April window."""
    return _sky_lookup([(contract, date(2026, 4, d), sky) for d in (1, 2, 3)])


def _mock_poll_response(voter_addresses: list[str]) -> MagicMock:
    """Return a mock for the polls/tally/{pollId} endpoint."""
    response = MagicMock()
    response.json.return_value = {"votesByAddress": [{"voter": addr} for addr in voter_addresses]}
    response.raise_for_status.return_value = None
    return response


_POLL = _poll("1234", date(2026, 4, 1), date(2026, 4, 3))
_CLOSED_POLL_NOW = datetime(2026, 4, 10, 17, 0, tzinfo=UTC)  # after the poll closes 2026-04-03 16:00

# ---------------------------------------------------------------------------
# poll_statuses — per-poll vote status, voter-set boundary normalization
# ---------------------------------------------------------------------------


def test_poll_statuses_has_an_entry_per_delegate_per_poll():
    delegates = [_delegate("Alice", _ADDR_A)]
    polls = [_POLL, _poll("5678", date(2026, 4, 1), date(2026, 4, 3))]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        out = sky_polling.poll_statuses(polls, delegates, _sky_throughout(_ADDR_A, 1000.0), _CLOSED_POLL_NOW)

    assert set(out) == {(_ADDR_A, "1234"), (_ADDR_A, "5678")}


def test_poll_statuses_normalizes_voter_address_case():
    """Mixed-case voter addresses from the API are lowercased at the boundary."""
    delegates = [_delegate("Alice", _ADDR_A)]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([_ADDR_A.upper().replace("0X", "0x")])
        out = sky_polling.poll_statuses([_POLL], delegates, _sky_throughout(_ADDR_A, 1000.0), _CLOSED_POLL_NOW)

    assert out[_ADDR_A, "1234"] == "Yes"


def test_poll_statuses_not_started_if_poll_ended_before_delegate_start():
    """If the poll closed before the delegate's start date, the status is overridden to 'Not Started'."""
    delegates = [_delegate("Alice", _ADDR_A, start=date(2026, 5, 1))]

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([])
        out = sky_polling.poll_statuses([_POLL], delegates, _sky_lookup([]), _CLOSED_POLL_NOW)

    assert out[_ADDR_A, "1234"] == "Not Started"


def test_poll_statuses_empty_polls_returns_empty_without_http():
    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        out = sky_polling.poll_statuses([], [_delegate("Alice", _ADDR_A)], _sky_lookup([]), _CLOSED_POLL_NOW)

    assert out == {}
    mock_session.assert_not_called()


def test_poll_statuses_multiple_delegates_per_poll():
    """Each delegate gets its own status; only the voter is marked Yes."""
    delegates = [_delegate("Alice", _ADDR_A), _delegate("Bob", _ADDR_B)]
    sky_lookup = _sky_throughout(_ADDR_A, 1000.0) | _sky_throughout(_ADDR_B, 500.0)

    with patch("ad_voting_metrics.sources.sky_polling.get_session") as mock_session:
        mock_session.return_value.get.return_value = _mock_poll_response([_ADDR_A])
        out = sky_polling.poll_statuses([_POLL], delegates, sky_lookup, _CLOSED_POLL_NOW)

    assert out == {(_ADDR_A, "1234"): "Yes", (_ADDR_B, "1234"): "No"}


# ---------------------------------------------------------------------------
# fetch_polls_for_period — pagination and date filtering against vote.sky.money
# ---------------------------------------------------------------------------


def _poll_dict(poll_id: int, start_iso: str, end_iso: str, title: str = "Test poll") -> dict:
    """Return an API-shaped poll dict with ISO startDate/endDate strings."""
    return {"pollId": poll_id, "startDate": start_iso, "endDate": end_iso, "title": title}


@responses.activate
def test_fetch_polls_for_period_single_page_filters_to_period():
    """Polls outside the period are filtered; the in-period poll comes back as a Ballot with typed dates."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 1},
            "polls": [
                _poll_dict(102, "2025-03-30T00:00:00Z", "2025-04-02T16:00:00Z", "Before window"),
                _poll_dict(101, "2025-04-05T00:00:00Z", "2025-04-08T16:00:00Z", "In window"),
                _poll_dict(103, "2025-05-02T00:00:00Z", "2025-05-05T16:00:00Z", "After window"),
            ],
        },
        status=200,
    )

    result = sky_polling.fetch_polls_for_period(period)

    assert result == [
        Ballot(id="101", kind="poll", start=date(2025, 4, 5), end=date(2025, 4, 8), title="In window"),
    ]


@responses.activate
def test_fetch_polls_for_period_paginates_until_numpages_reached():
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

    result = sky_polling.fetch_polls_for_period(period)

    assert [p.id for p in result] == ["201", "202"]
    assert len(responses.calls) == 2
    url_1, url_2 = responses.calls[0].request.url, responses.calls[1].request.url
    assert url_1 is not None
    assert url_2 is not None
    assert "page=1" in url_1
    assert "page=2" in url_2


@responses.activate
def test_fetch_polls_for_period_stops_at_first_poll_after_period():
    """Oldest-first listing: a poll starting after the period ends paging, even with more pages advertised."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={
            "paginationInfo": {"numPages": 3},
            "polls": [
                _poll_dict(401, "2025-04-28T00:00:00Z", "2025-05-01T16:00:00Z", "Last in period"),
                _poll_dict(402, "2025-05-05T00:00:00Z", "2025-05-08T16:00:00Z", "After period"),
            ],
        },
        status=200,
    )

    result = sky_polling.fetch_polls_for_period(period)

    assert [p.id for p in result] == ["401"]
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_polls_for_period_stops_on_empty_pagination_info():
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

    result = sky_polling.fetch_polls_for_period(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_polls_for_period_stops_on_empty_polls_list():
    """Empty polls list terminates the loop without raising."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 5}, "polls": []},
        status=200,
    )

    result = sky_polling.fetch_polls_for_period(period)

    assert result == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_polls_for_period_request_url_includes_period_start():
    """The startDate query parameter is the period's first day in ISO form."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_polling.SKY_ALL_POLLS_URL,
        json={"paginationInfo": {"numPages": 1}, "polls": []},
        status=200,
    )

    sky_polling.fetch_polls_for_period(period)

    url = responses.calls[0].request.url
    assert url is not None
    assert "startDate=2025-04-01" in url
    assert "network=mainnet" in url
    assert f"pageSize={sky_polling.SKY_POLL_PAGE_SIZE}" in url
