"""Constants and factories shared across the test modules."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import responses
from requests.exceptions import HTTPError

from ad_voting_metrics.roster import Delegate

ADDR_A = "0x" + "a" * 40
ADDR_B = "0x" + "b" * 40
ADDR_C = "0x" + "c" * 40


def delegate(
    name: str = "Alice",
    address: str = ADDR_A,
    start: date = date(2024, 1, 1),
    end: date | None = None,
) -> Delegate:
    return Delegate(name=name, vote_delegate_address=address, start_date=start, end_date=end)


def ts(day: date) -> int:
    """Unix timestamp for midnight UTC on `day`."""
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


def http_429() -> HTTPError:
    """Build the HTTPError a provider raises when it rate-limits a request."""
    response = MagicMock()
    response.status_code = 429
    return HTTPError("429 Client Error: Too Many Requests", response=response)


def request_urls() -> list[str]:
    """URLs of the requests the active `responses` mock has served, in order."""
    return [call.request.url or "" for call in responses.calls]
