"""Tests for sources.delegates — the vote.sky.money paginated fetcher."""

import pytest
import responses
from requests.exceptions import HTTPError

from ad_voting_metrics.sources.delegates import DELEGATES_URL, fetch_aligned_delegates
from ad_voting_metrics.sources.http import MAX_PAGES
from tests.helpers import ADDR_A, ADDR_B, ADDR_C, request_urls


def _entry(name: str, address: str) -> dict:
    return {"name": name, "voteDelegateAddress": address}


def _add_page(delegates: list[dict]) -> None:
    responses.add(
        responses.GET, DELEGATES_URL, json={"paginationInfo": {"hasNextPage": bool(delegates)}, "delegates": delegates}
    )


@responses.activate
def test_returns_every_delegate_across_pages_and_requests_aligned_mainnet_delegates():
    _add_page([_entry("Alpha", ADDR_A), _entry("Beta", ADDR_B)])
    _add_page([_entry("Gamma", ADDR_C)])
    _add_page([])

    result = fetch_aligned_delegates()

    assert [d["name"] for d in result] == ["Alpha", "Beta", "Gamma"]
    urls = request_urls()
    assert len(urls) == 3
    assert "page=1" in urls[0]
    assert "page=2" in urls[1]
    assert "page=3" in urls[2]
    assert "delegateType=ALIGNED" in urls[0]
    assert "network=mainnet" in urls[0]


@responses.activate
def test_empty_first_page_returns_nothing():
    _add_page([])

    assert fetch_aligned_delegates() == []
    assert len(responses.calls) == 1


@responses.activate
def test_page_cap_stops_runaway_pagination(caplog):
    """An API that never returns an empty page stops at MAX_PAGES with a warning."""
    for _ in range(MAX_PAGES):
        _add_page([_entry("X", ADDR_A)])

    with caplog.at_level("WARNING"):
        result = fetch_aligned_delegates()

    assert len(result) == MAX_PAGES
    assert len(responses.calls) == MAX_PAGES
    assert "results may be incomplete" in caplog.text


@responses.activate
def test_http_error_raises():
    responses.add(responses.GET, DELEGATES_URL, json={"error": "not found"}, status=404)

    with pytest.raises(HTTPError):
        fetch_aligned_delegates()
