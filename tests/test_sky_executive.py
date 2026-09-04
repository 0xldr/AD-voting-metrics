"""Tests for sources.sky_executive — vote.sky.money executive endpoints."""

from datetime import date
from unittest.mock import patch

import responses

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.sources import sky_executive

_ADDR_A = "0x" + "a" * 40
_SPELL_1 = "0x" + "11" * 20
_SPELL_2 = "0x" + "22" * 20
_LIVE = date(2026, 4, 5)


def _delegate(name: str, address: str, start: date = date(2024, 1, 1)) -> Delegate:
    return Delegate(name=name, vote_delegate_address=address, start_date=start)


def _spell(address: str, start: date = _LIVE) -> Ballot:
    return Ballot(id=address, kind="spell", start=start, end=None, title="Test spell")


# ---------------------------------------------------------------------------
# spell_statuses — balance-derived seed statuses
# ---------------------------------------------------------------------------


def test_spell_statuses_has_an_entry_per_delegate_per_spell():
    delegates = [_delegate("Alice", _ADDR_A)]
    sky_lookup = {(_ADDR_A, _LIVE): 1000.0}

    out = sky_executive.spell_statuses([_spell(_SPELL_1), _spell(_SPELL_2)], delegates, sky_lookup)

    assert set(out) == {(_ADDR_A, _SPELL_1), (_ADDR_A, _SPELL_2)}


def test_spell_statuses_with_sky_returns_pending():
    """Non-zero SKY on the live day → 'Pending verification', awaiting on-chain timing."""
    out = sky_executive.spell_statuses([_spell(_SPELL_1)], [_delegate("Alice", _ADDR_A)], {(_ADDR_A, _LIVE): 1000.0})

    assert out[_ADDR_A, _SPELL_1] == "Pending verification"


def test_spell_statuses_zero_sky_returns_no_delegated_sky():
    out = sky_executive.spell_statuses([_spell(_SPELL_1)], [_delegate("Alice", _ADDR_A)], {(_ADDR_A, _LIVE): 0.0})

    assert out[_ADDR_A, _SPELL_1] == "No Delegated SKY"


def test_spell_statuses_not_started_if_spell_went_live_before_delegate_start():
    """A delegate aligned after the spell went live is 'Not Started' regardless of balance."""
    delegates = [_delegate("Alice", _ADDR_A, start=date(2026, 5, 1))]

    out = sky_executive.spell_statuses([_spell(_SPELL_1)], delegates, {(_ADDR_A, _LIVE): 1000.0})

    assert out[_ADDR_A, _SPELL_1] == "Not Started"


def test_spell_statuses_makes_no_http_call():
    """Seed statuses come from SKY balances alone.

    The supporters endpoint reports only who currently supports a spell, with no vote timestamp, so it cannot answer
    the deadline question and is deliberately not consulted.
    """
    with patch("ad_voting_metrics.sources.sky_executive.get_session") as mock_session:
        sky_executive.spell_statuses([_spell(_SPELL_1)], [_delegate("Alice", _ADDR_A)], {(_ADDR_A, _LIVE): 1000.0})

    mock_session.assert_not_called()


def test_spell_statuses_empty_spells_returns_empty():
    assert sky_executive.spell_statuses([], [_delegate("Alice", _ADDR_A)], {}) == {}


# ---------------------------------------------------------------------------
# fetch_spells_for_period — pagination and date filtering against vote.sky.money
# ---------------------------------------------------------------------------


def _executive_dict(address: str, date_iso: str, title: str = "Test spell") -> dict:
    """Return an API-shaped executive dict."""
    return {"address": address, "date": date_iso, "title": title}


@responses.activate
def test_fetch_spells_for_period_filters_to_period_and_stops_at_older_spell():
    """Newest-first listing: later spells skipped, the in-period one kept, paging stops at the first older one."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_executive.SKY_EXECUTIVE_URL,
        json=[
            _executive_dict("0xCCCC000000000000000000000000000000000003", "2025-06-10T00:00:00Z", "After"),
            _executive_dict("0xAAAA000000000000000000000000000000000001", "2025-04-10T00:00:00Z", "In window"),
            _executive_dict("0xBBBB000000000000000000000000000000000002", "2025-02-10T00:00:00Z", "Before"),
        ],
        status=200,
    )

    result = sky_executive.fetch_spells_for_period(period)

    assert result == [
        Ballot(
            id="0xaaaa000000000000000000000000000000000001",
            kind="spell",
            start=date(2025, 4, 10),
            end=None,
            title="In window",
        ),
    ]
    # The pre-period spell ended paging; no second request was made.
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_spells_for_period_advances_start_until_empty():
    """The `start` query advances by SKY_EXECUTIVES_PAGE_SIZE until the API returns []."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(
        responses.GET,
        sky_executive.SKY_EXECUTIVE_URL,
        json=[_executive_dict("0xspell0000000000000000000000000000000002", "2025-04-22T00:00:00Z")],
        status=200,
    )
    responses.add(
        responses.GET,
        sky_executive.SKY_EXECUTIVE_URL,
        json=[_executive_dict("0xspell0000000000000000000000000000000001", "2025-04-05T00:00:00Z")],
        status=200,
    )
    responses.add(responses.GET, sky_executive.SKY_EXECUTIVE_URL, json=[], status=200)

    result = sky_executive.fetch_spells_for_period(period)

    assert len(result) == 2
    assert len(responses.calls) == 3
    page_size = sky_executive.SKY_EXECUTIVES_PAGE_SIZE
    url_0, url_1, url_2 = (c.request.url for c in responses.calls)
    assert url_0 is not None
    assert url_1 is not None
    assert url_2 is not None
    assert "start=0" in url_0
    assert f"start={page_size}" in url_1
    assert f"start={page_size * 2}" in url_2


@responses.activate
def test_fetch_spells_for_period_empty_first_page_returns_empty():
    """An empty first-page response terminates immediately and returns []."""
    period = MonthPeriod(year=2025, month=4)
    responses.add(responses.GET, sky_executive.SKY_EXECUTIVE_URL, json=[], status=200)

    result = sky_executive.fetch_spells_for_period(period)

    assert result == []
    assert len(responses.calls) == 1
