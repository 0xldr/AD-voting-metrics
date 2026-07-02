"""Shared fixtures for the test suite."""

import pytest

from ad_voting_metrics.sources import http as http_module


@pytest.fixture(autouse=True)
def _reset_session():
    """Clear the cached requests.Session around every test.

    Modules under test share one cached Session; without this, a Session created under one test's mocked HTTP
    transport would leak into the next test.
    """
    http_module.get_session.cache_clear()
    yield
    http_module.get_session.cache_clear()
