"""HTTP access to vote.sky.money: one shared session, one JSON getter, one pagination loop.

A single module-cached `requests.Session` is reused across all API calls, giving connection pooling, uniform retry
behaviour, and one place to adjust timeouts and headers.
"""

import itertools
import logging
from collections.abc import Callable, Iterator
from functools import cache
from importlib.metadata import version
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# (connect_timeout, read_timeout) in seconds. Without this, requests waits forever on a hung connection.
_HTTP_TIMEOUT = (5, 30)

# Transient HTTP statuses worth retrying. 429 (rate limit) and 5xx.
_RETRY_STATUSES = (429, 500, 502, 503, 504)

# Pages fetched from one listing before giving up, so an API that never runs dry cannot spin forever.
MAX_PAGES = 50


@cache
def get_session() -> requests.Session:
    """Return the shared requests.Session, creating it on first call.

    Retries transient failures (timeouts, connection errors, 5xx, 429) up to 3 times with exponential backoff.
    Persistent failures raise requests.exceptions.RetryError. Only idempotent methods are retried (urllib3's default).
    """
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1,
        status_forcelist=_RETRY_STATUSES,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {"User-Agent": f"ad-voting-metrics/{version('ad-voting-metrics')}", "Accept": "application/json"}
    )
    return session


def fetch_json(url: str, **params: str | int) -> Any:  # noqa: ANN401 — the body is whatever JSON the endpoint returns
    """GET `url` with the query params and return the decoded JSON body; HTTP error statuses raise."""
    response = get_session().get(url, params=params, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    return response.json()


def paginate(
    fetch_page: Callable[[int], list[dict[str, Any]]], *, first: int = 1, step: int = 1
) -> Iterator[list[dict[str, Any]]]:
    """Yield successive pages from fetch_page(first), fetch_page(first + step), ... until one comes back empty.

    Stops with a warning after MAX_PAGES pages so a listing that never empties cannot loop forever.
    """
    for number in itertools.islice(itertools.count(first, step), MAX_PAGES):
        page = fetch_page(number)
        if not page:
            return
        yield page
    logger.warning("Stopped paging %s after %d pages; results may be incomplete", fetch_page.__name__, MAX_PAGES)
