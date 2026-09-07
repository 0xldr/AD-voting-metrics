"""Tests for sources.chain — batched block-timestamp fetching with rate-limit backoff."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from requests.exceptions import HTTPError
from web3.exceptions import Web3RPCError

from ad_voting_metrics.sources import chain
from tests.helpers import ts


def test_utc_date_returns_the_utc_calendar_day():
    assert chain.utc_date(ts(date(2026, 4, 1)) + 23 * 3600) == date(2026, 4, 1)


def test_fetch_block_timestamps_uses_one_batch_for_missing_blocks():
    mock_w3 = MagicMock()
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.return_value = [{"timestamp": 1_700_000_000}, {"timestamp": 1_700_000_100}]
    timestamps: dict[int, int] = {}

    chain.fetch_block_timestamps(mock_w3, {5, 6}, timestamps)

    assert timestamps == {5: 1_700_000_000, 6: 1_700_000_100}
    assert batch.add.call_count == 2


def test_fetch_block_timestamps_falls_back_to_sequential_calls():
    """Providers without JSON-RPC batch support get one get_block call per block."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.side_effect = ValueError("batch not supported")
    mock_w3.eth.get_block.side_effect = [{"timestamp": 1}, {"timestamp": 2}]
    timestamps: dict[int, int] = {}

    chain.fetch_block_timestamps(mock_w3, {5, 6}, timestamps)

    assert timestamps == {5: 1, 6: 2}
    assert mock_w3.eth.get_block.call_count == 2


def test_fetch_block_timestamps_raises_when_the_sequential_fallback_also_fails():
    mock_w3 = MagicMock()
    mock_w3.batch_requests.side_effect = ValueError("batch not supported")
    mock_w3.eth.get_block.side_effect = Web3RPCError("node down")

    with pytest.raises(RuntimeError, match=r"get_block\(5\) failed"):
        chain.fetch_block_timestamps(mock_w3, {5}, {})


def test_safe_head_stays_finality_blocks_behind_the_tip():
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 1_000

    assert chain.safe_head(mock_w3) == 1_000 - chain.FINALITY_BLOCKS


def test_fetch_block_timestamps_skips_cached_blocks():
    """No RPC traffic when every block already has a cached timestamp."""
    mock_w3 = MagicMock()
    timestamps = {5: 42}

    chain.fetch_block_timestamps(mock_w3, {5}, timestamps)

    assert timestamps == {5: 42}
    mock_w3.batch_requests.assert_not_called()


def _http_429() -> HTTPError:
    """Build the HTTPError a provider raises when it rate-limits a request."""
    response = MagicMock()
    response.status_code = 429
    return HTTPError("429 Client Error: Too Many Requests", response=response)


def test_fetch_block_timestamps_retries_batch_after_rate_limit():
    """A rate-limited batch is retried after a backoff sleep."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [_http_429(), [{"timestamp": 7}]]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, {5}, timestamps)

    assert timestamps == {5: 7}
    mock_sleep.assert_called_once_with(chain.RATE_LIMIT_BASE_DELAY_SECONDS)


def test_fetch_block_timestamps_raises_after_persistent_rate_limit():
    """Rate limiting on every attempt exhausts the retry budget and surfaces the HTTPError."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [_http_429() for _ in range(chain.RATE_LIMIT_ATTEMPTS)]

    with patch("ad_voting_metrics.sources.chain.time.sleep"), pytest.raises(HTTPError):
        chain.fetch_block_timestamps(mock_w3, {5}, {})

    assert batch.execute.call_count == chain.RATE_LIMIT_ATTEMPTS


def test_fetch_block_timestamps_splits_large_sets_into_multiple_batches():
    total = chain.TIMESTAMP_BATCH_SIZE + 1
    mock_w3 = MagicMock()
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [
        [{"timestamp": i} for i in range(chain.TIMESTAMP_BATCH_SIZE)],
        [{"timestamp": chain.TIMESTAMP_BATCH_SIZE}],
    ]
    timestamps: dict[int, int] = {}

    chain.fetch_block_timestamps(mock_w3, set(range(total)), timestamps)

    assert len(timestamps) == total
    assert mock_w3.batch_requests.call_count == 2
