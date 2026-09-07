"""Tests for sources.sky_polling — vote.sky.money polling endpoints."""

from datetime import UTC, date, datetime

import pytest
import responses

from ad_voting_metrics.ballots import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import sky_polling
from tests.helpers import ADDR_A, ADDR_B, delegate, request_urls, ts

_POLL = Ballot(id="1234", start=date(2026, 4, 1), end=date(2026, 4, 3), title="Test poll")
_AFTER_CLOSE = datetime(2026, 4, 10, 17, 0, tzinfo=UTC)


def _sky_throughout(contract: str, sky: float) -> dict[tuple[str, date], float]:
    """Constant balance across the poll's 1-3 April window."""
    return {(contract, date(2026, 4, d)): sky for d in (1, 2, 3)}


def _add_tally(poll_id: str, votes: dict[str, date]) -> None:
    """Register the tally for `poll_id`; `votes` maps voter address to the day the vote was recorded."""
    responses.add(
        responses.GET,
        f"{sky_polling.SKY_POLL_ID_URL}/{poll_id}",
        json={"votesByAddress": [{"voter": v, "blockTimestamp": str(ts(day))} for v, day in votes.items()]},
    )


@responses.activate
def test_poll_statuses_has_an_entry_per_delegate_per_poll():
    second = Ballot(id="5678", start=date(2026, 4, 1), end=date(2026, 4, 3), title="Test poll")
    _add_tally("1234", {})
    _add_tally("5678", {})

    out = sky_polling.poll_statuses([_POLL, second], [delegate()], _sky_throughout(ADDR_A, 1000.0), _AFTER_CLOSE)

    assert set(out) == {(ADDR_A, "1234"), (ADDR_A, "5678")}


@responses.activate
def test_poll_statuses_normalizes_voter_address_case():
    """Mixed-case voter addresses from the API are lowercased at the boundary."""
    _add_tally("1234", {ADDR_A.upper().replace("0X", "0x"): date(2026, 4, 2)})

    out = sky_polling.poll_statuses([_POLL], [delegate()], _sky_throughout(ADDR_A, 1000.0), _AFTER_CLOSE)

    assert out[ADDR_A, "1234"] == "Yes"


@responses.activate
def test_poll_statuses_not_started_if_poll_closed_before_delegate_start():
    _add_tally("1234", {})

    out = sky_polling.poll_statuses([_POLL], [delegate(start=date(2026, 5, 1))], {}, _AFTER_CLOSE)

    assert out[ADDR_A, "1234"] == "Not Started"


@responses.activate
@pytest.mark.parametrize(
    ("end", "vote_day", "expected"),
    [
        pytest.param(date(2026, 3, 31), None, "Exited", id="left before the poll opened"),
        pytest.param(date(2026, 4, 2), None, "Exited", id="left mid-window without voting"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 3), "Exited", id="left mid-window, voted after leaving"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 2), "Yes", id="left mid-window, voted on the last aligned day"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 1), "Yes", id="left mid-window, voted while aligned"),
        pytest.param(date(2026, 4, 3), None, "No", id="left on the close day counts as a full window"),
        pytest.param(date(2026, 4, 30), None, "No", id="left after the poll closed"),
    ],
)
def test_poll_statuses_for_a_delegate_who_exits(end, vote_day, expected):
    """A vote cast on or before the inclusive last aligned day counts; otherwise leaving before close is Exited."""
    _add_tally("1234", {ADDR_A: vote_day} if vote_day else {})

    out = sky_polling.poll_statuses([_POLL], [delegate(end=end)], _sky_throughout(ADDR_A, 1000.0), _AFTER_CLOSE)

    assert out[ADDR_A, "1234"] == expected


@responses.activate
def test_poll_statuses_rejects_a_poll_without_an_end_date():
    _add_tally("9", {})
    open_ended = Ballot(id="9", start=date(2026, 4, 1), end=None, title="No end")

    with pytest.raises(ValueError, match="Poll 9 has no end date"):
        sky_polling.poll_statuses([open_ended], [delegate()], {}, _AFTER_CLOSE)


@responses.activate
def test_poll_statuses_empty_polls_returns_empty_without_http():
    assert sky_polling.poll_statuses([], [delegate()], {}, _AFTER_CLOSE) == {}
    assert len(responses.calls) == 0


@responses.activate
def test_poll_statuses_multiple_delegates_per_poll():
    """Each delegate gets its own status; only the voter is marked Yes."""
    _add_tally("1234", {ADDR_A: date(2026, 4, 2)})
    sky_lookup = _sky_throughout(ADDR_A, 1000.0) | _sky_throughout(ADDR_B, 500.0)

    out = sky_polling.poll_statuses([_POLL], [delegate(), delegate("Bob", ADDR_B)], sky_lookup, _AFTER_CLOSE)

    assert out == {(ADDR_A, "1234"): "Yes", (ADDR_B, "1234"): "No"}


def _poll_dict(poll_id: int, start_iso: str, end_iso: str, title: str = "Test poll") -> dict:
    return {"pollId": poll_id, "startDate": start_iso, "endDate": end_iso, "title": title}


def _add_page(polls: list[dict]) -> None:
    responses.add(responses.GET, sky_polling.SKY_ALL_POLLS_URL, json={"polls": polls})


@responses.activate
def test_fetch_polls_for_period_single_page_filters_to_period():
    """Polls outside the period are filtered; the in-period poll comes back as a Ballot with typed dates."""
    _add_page(
        [
            _poll_dict(102, "2025-03-30T00:00:00Z", "2025-04-02T16:00:00Z", "Before window"),
            _poll_dict(101, "2025-04-05T00:00:00Z", "2025-04-08T16:00:00Z", "In window"),
            _poll_dict(103, "2025-05-02T00:00:00Z", "2025-05-05T16:00:00Z", "After window"),
        ],
    )

    result = sky_polling.fetch_polls_for_period(MonthPeriod(2025, 4))

    assert result == [Ballot(id="101", start=date(2025, 4, 5), end=date(2025, 4, 8), title="In window")]


@responses.activate
def test_fetch_polls_for_period_paginates_until_an_empty_page():
    _add_page([_poll_dict(201, "2025-04-02T00:00:00Z", "2025-04-05T16:00:00Z")])
    _add_page([_poll_dict(202, "2025-04-20T00:00:00Z", "2025-04-23T16:00:00Z")])
    _add_page([])

    result = sky_polling.fetch_polls_for_period(MonthPeriod(2025, 4))

    assert [p.id for p in result] == ["201", "202"]
    urls = request_urls()
    assert len(urls) == 3
    assert "page=1" in urls[0]
    assert "page=2" in urls[1]
    assert "page=3" in urls[2]


@responses.activate
def test_fetch_polls_for_period_stops_at_first_poll_after_period():
    """Oldest-first listing: a poll starting after the period ends paging, even with more pages advertised."""
    _add_page(
        [
            _poll_dict(401, "2025-04-28T00:00:00Z", "2025-05-01T16:00:00Z", "Last in period"),
            _poll_dict(402, "2025-05-05T00:00:00Z", "2025-05-08T16:00:00Z", "After period"),
        ],
    )

    result = sky_polling.fetch_polls_for_period(MonthPeriod(2025, 4))

    assert [p.id for p in result] == ["401"]
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_polls_for_period_stops_on_empty_polls_list():
    _add_page([])

    assert sky_polling.fetch_polls_for_period(MonthPeriod(2025, 4)) == []
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_polls_for_period_request_url_includes_period_start():
    _add_page([])

    sky_polling.fetch_polls_for_period(MonthPeriod(2025, 4))

    (url,) = request_urls()
    assert "startDate=2025-04-01" in url
    assert "network=mainnet" in url
    assert f"pageSize={sky_polling.SKY_POLL_PAGE_SIZE}" in url
