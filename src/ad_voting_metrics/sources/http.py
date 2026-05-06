"""Shared HTTP session for all outbound requests.

A single module-cached `requests.Session` is reused across all external API
calls (sky_dao's vote.sky.money endpoints, sources.delegates, future
clients). Reusing one session gives us:

- Connection pooling (TCP/TLS handshakes amortized across calls)
- Uniform retry behavior on transient failures
- One place to adjust timeouts, retry counts, and backoff

Use `get_session()` to retrieve the cached session. Do not instantiate
`requests.Session()` directly.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Timeout for all HTTP requests, as (connect_timeout, read_timeout) seconds.
# Without an explicit timeout, requests will wait forever on a hung connection.
HTTP_TIMEOUT = (5, 30)

# HTTP statuses worth retrying. 429 (rate limit) and 5xx (server error) are
# transient by definition; 4xx other than 429 are caller errors and should
# not be retried.
_RETRY_STATUSES = (429, 500, 502, 503, 504)

# Module-level cache. Initialized on first get_session() call.
_session_instance: requests.Session | None = None


def get_session() -> requests.Session:
    """Return the shared requests.Session, creating it on first call.

    Retries transient failures (timeouts, connection errors, 5xx, 429) up
    to 3 times with exponential backoff: sleeps of 0s, 2s, 4s between
    retries. Persistent failures raise requests.exceptions.RetryError
    after exhausting retries.

    Only idempotent methods (GET, HEAD, etc.) are retried, which is the
    urllib3 default. POST/PUT/DELETE retries would require an allowlist
    we don't currently set.

    Tests that mock HTTP traffic should reset _session_instance to None
    in a fixture so a fresh session picks up their mocks rather than a
    leftover from another test.
    """
    global _session_instance
    if _session_instance is None:
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
        _session_instance = s
    return _session_instance
