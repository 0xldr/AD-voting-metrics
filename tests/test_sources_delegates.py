"""Tests for sources.delegates — the vote.sky.money paginated fetcher."""

import pytest
import requests
import responses

from ad_voting_metrics.sources import http as http_module
from ad_voting_metrics.sources.delegates import _MAX_PAGES, DELEGATES_URL, fetch_aligned_delegates

# Errors a misbehaving HTTP endpoint can surface through urllib3's
# retry layer + requests' raise_for_status.
_HTTP_FAILURES = (
    requests.exceptions.HTTPError,
    requests.exceptions.RetryError,
    requests.exceptions.ConnectionError,
)


@pytest.fixture(autouse=True)
def reset_session():
    """Clear sources.http's cached Session before/after each test to prevent state leakage."""
    http_module.get_session.cache_clear()
    yield
    http_module.get_session.cache_clear()


def _delegate_dict(name: str, address: str) -> dict:
    """Return a minimal API-shaped delegate dict for tests."""
    return {
        "name": name,
        "voteDelegateAddress": address,
        "address": "0x0000000000000000000000000000000000000000",
        "status": "aligned",
        "creationDate": "2024-01-01T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Single-page response: hasNextPage=false on first page
# ---------------------------------------------------------------------------


@responses.activate
def test_single_page_returns_all_delegates():
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": False},
            "stats": {"total": 2, "aligned": 2},
            "delegates": [
                _delegate_dict("Alpha", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                _delegate_dict("Beta", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ],
        },
        status=200,
    )

    result = fetch_aligned_delegates()
    assert len(result) == 2
    assert result[0]["name"] == "Alpha"
    assert result[1]["name"] == "Beta"


@responses.activate
def test_empty_response():
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": False},
            "stats": {"total": 0, "aligned": 0},
            "delegates": [],
        },
        status=200,
    )

    result = fetch_aligned_delegates()
    assert result == []


# ---------------------------------------------------------------------------
# Multi-page response: hasNextPage=true then false
# ---------------------------------------------------------------------------


@responses.activate
def test_multi_page_concatenates_results():
    # Page 1: hasNextPage=true
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": True},
            "stats": {"total": 3, "aligned": 3},
            "delegates": [
                _delegate_dict("Alpha", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                _delegate_dict("Beta", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ],
        },
        status=200,
    )
    # Page 2: hasNextPage=false
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 2, "numPages": None, "hasNextPage": False},
            "stats": {"total": 3, "aligned": 3},
            "delegates": [
                _delegate_dict("Gamma", "0xcccccccccccccccccccccccccccccccccccccccccc"),
            ],
        },
        status=200,
    )

    result = fetch_aligned_delegates()
    assert len(result) == 3
    assert [d["name"] for d in result] == ["Alpha", "Beta", "Gamma"]


@responses.activate
def test_pagination_increments_page_param():
    """Verify the page number actually advances across requests."""
    # Page 1 needs at least one delegate so we don't hit the empty page stop.
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": True},
            "stats": {"total": 1, "aligned": 1},
            "delegates": [_delegate_dict("Alpha", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")],
        },
        status=200,
    )
    # Page 2 hasNextPage=false so ends the loop normally
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 2, "numPages": None, "hasNextPage": False},
            "stats": {"total": 1, "aligned": 1},
            "delegates": [_delegate_dict("Beta", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")],
        },
        status=200,
    )

    fetch_aligned_delegates()

    # Two requests made, in order
    assert len(responses.calls) == 2
    # page= query should be 1, then 2
    url_1 = responses.calls[0].request.url
    url_2 = responses.calls[1].request.url
    assert url_1 is not None
    assert url_2 is not None
    assert "page=1" in url_1
    assert "page=2" in url_2


@responses.activate
def test_query_params_include_aligned_filter():
    """Verify the aligned=true filter is included."""
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": False},
            "stats": {"total": 0, "aligned": 0},
            "delegates": [],
        },
        status=200,
    )

    fetch_aligned_delegates()
    url = responses.calls[0].request.url
    assert url is not None
    assert "delegateType=ALIGNED" in url
    assert "network=mainnet" in url


# ---------------------------------------------------------------------------
# Defensive: page cap prevents infinite loops on misbehaving APIs
# ---------------------------------------------------------------------------


@responses.activate
def test_stops_on_empty_page_even_when_hasnextpage_true():
    """Treat an empty page as end-of-data; sky.money returns delegates=[] with hasNextPage=true forever after last."""
    # Page 1: 2 delegates, hasNextPage = true
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": True},
            "stats": {"total": 2, "aligned": 2},
            "delegates": [
                _delegate_dict("Alpha", "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
                _delegate_dict("Beta", "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
            ],
        },
        status=200,
    )
    # Page 2: empty list, but hasNextPage still true
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={
            "paginationInfo": {"page": 2, "numPages": None, "hasNextPage": True},
            "stats": {"total": 2, "aligned": 2},
            "delegates": [],
        },
        status=200,
    )

    result = fetch_aligned_delegates()
    assert len(result) == 2
    assert len(responses.calls) == 2


@responses.activate
def test_page_cap_stops_infinite_loop(caplog: pytest.LogCaptureFixture):
    """If the API always returns hasNextPage=true, we should stop after 10 pages."""
    # Register enough responses to satisfy _MAX_PAGES, all with hasNextPage=true
    for _ in range(_MAX_PAGES):
        responses.add(
            responses.GET,
            DELEGATES_URL,
            json={
                "paginationInfo": {"page": 1, "numPages": None, "hasNextPage": True},
                "stats": {"total": 0, "aligned": 0},
                "delegates": [_delegate_dict("X", "0x" + "0" * 40)],
            },
            status=200,
        )

    with caplog.at_level("WARNING"):
        result = fetch_aligned_delegates()

    # Stopped after _MAX_PAGES, not infinite
    assert len(responses.calls) == _MAX_PAGES
    assert len(result) == _MAX_PAGES  # Got one delegate per page
    # The cap-hit warning fired
    assert any("page cap" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# HTTP error handling: relies on raise_for_status from the shared session
# ---------------------------------------------------------------------------


@responses.activate
def test_500_error_raises():
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={"error": "internal"},
        status=500,
    )
    # The session has retries on 5xx, but after retries are exhausted
    # raise_on_status=False means raise_for_status fires. We register only
    # one response, so retries will fail on subsequent attempts (responses
    # raises ConnectionError when no match exists). Either way, the test
    # confirms 500s don't get swallowed.
    with pytest.raises(_HTTP_FAILURES):
        fetch_aligned_delegates()


@responses.activate
def test_404_error_raises():
    responses.add(
        responses.GET,
        DELEGATES_URL,
        json={"error": "not found"},
        status=404,
    )
    # 404 isn't in the retry list, so raise_for_status fires immediately.
    with pytest.raises(_HTTP_FAILURES):
        fetch_aligned_delegates()
