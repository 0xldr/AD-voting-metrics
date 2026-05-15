"""Shared HTTP session for all outbound requests.

A single module-cached `requests.Session` is reused across all external
API calls, giving us connection pooling, uniform retry behavior, and
one placeto adjust timeouts. Use `get_session()`; don't instantiate
`requests.Session()` directly.
"""

from functools import cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# (connect_timeout, read_timeout) in seconds. Without this, requests
# will wait forever on a hung connection.
HTTP_TIMEOUT = (5, 30)

# Transiet HTTP statuses worth retrying. 429 (rate limit) and 5xx.
_RETRY_STATUSES = (429, 500, 502, 503, 504)


@cache
def get_session() -> requests.Session:
    """Return the shared requests.Session, creating it on first call.

    Retries transient failures (timeouts, connection errors, 5xx, 429)
    up to 3 times with exponential backoff. Persistent failures raise
    requests.exceptions.RetryError. Only idempotent methods are retried
    (urllib3's default).

    Tests that mock HTTP traffic should call `get_session.cache_clear()`
    in a fixture so a fresh session picks up their mocks.

    Returns:
        The cached Session.
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
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s
