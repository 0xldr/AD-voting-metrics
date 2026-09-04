"""Tests for sources.sky_executive — vote.sky.money executive endpoints."""

from datetime import date
from unittest.mock import patch

import pandas as pd
import responses

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import sky_executive


# Helper for building sky_lookup dicts in sky_executive tests.
def _sky_lookup(rows: list[tuple[str, date, float]]) -> dict[tuple[str, date], float]:
    return {(contract, day): sky for contract, day, sky in rows}


# ---------------------------------------------------------------------------
# add_spell_vote_statuses — balance-derived seed statuses
# ---------------------------------------------------------------------------


def test_add_spell_vote_statuses_adds_column_per_spell():
    """Each spell in spell_info gets its own column on df, keyed by spell address."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        ]
    )
    sky_lookup = _sky_lookup([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [
        {"address": "0xspell1", "startDate": date(2026, 4, 5)},
        {"address": "0xspell2", "startDate": date(2026, 4, 5)},
    ]

    result = sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    assert "0xspell1" in result.columns
    assert "0xspell2" in result.columns


def test_add_spell_vote_statuses_with_sky_returns_pending():
    """Non-zero SKY on startDate → 'Pending verification', awaiting on-chain timing."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        ]
    )
    sky_lookup = _sky_lookup([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    result = sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    assert result.loc[0, "0xspell1"] == "Pending verification"


def test_add_spell_vote_statuses_zero_sky_returns_no_delegated_sky():
    """SKY balance is 0 on startDate → 'No Delegated SKY'."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        ]
    )
    sky_lookup = _sky_lookup([("0xaaa", date(2026, 4, 5), 0.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    result = sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    assert result.loc[0, "0xspell1"] == "No Delegated SKY"


def test_add_spell_vote_statuses_not_started_if_spell_started_before_delegate():
    """spell startDate < delegate Start Date → 'Not Started' (overrides everything)."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2026-05-01"},
        ]
    )
    sky_lookup = _sky_lookup([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    result = sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    assert result.loc[0, "0xspell1"] == "Not Started"


def test_add_spell_vote_statuses_makes_no_http_call():
    """Seed statuses come from SKY balances alone.

    The supporters endpoint reports only who currently supports a spell, with no vote timestamp, so it cannot answer
    the deadline question and is deliberately not consulted.
    """
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        ]
    )
    sky_lookup = _sky_lookup([("0xaaa", date(2026, 4, 5), 1000.0)])
    spell_info = [{"address": "0xspell1", "startDate": date(2026, 4, 5)}]

    with patch("ad_voting_metrics.sources.sky_executive.get_session") as mock_session:
        sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    mock_session.assert_not_called()


def test_add_spell_vote_statuses_empty_spell_info_leaves_df_unchanged():
    """No spells → df is returned unchanged."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": "2024-01-01"},
        ]
    )
    sky_lookup = _sky_lookup([])
    original_columns = list(df.columns)

    result = sky_executive.add_spell_vote_statuses([], df, sky_lookup)

    assert list(result.columns) == original_columns


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

    assert len(result) == 1
    spell = result[0]
    assert spell["address"] == "0xaaaa000000000000000000000000000000000001"
    assert spell["startDate"] == date(2025, 4, 10)
    assert spell["title"] == "In window"
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
