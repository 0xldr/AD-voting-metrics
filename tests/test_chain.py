"""Tests for sources.chain — batched block-timestamp fetching with rate-limit backoff."""

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from web3.exceptions import BadResponseFormat, Web3RPCError

from ad_voting_metrics.sources import chain
from tests.helpers import http_429, ts


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


def test_fetch_block_timestamps_retries_batch_after_rate_limit():
    """A rate-limited batch is retried after a backoff sleep."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [http_429(), [{"timestamp": 7}]]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, {5}, timestamps)

    assert timestamps == {5: 7}
    mock_sleep.assert_called_once_with(chain.RATE_LIMIT_BASE_DELAY_SECONDS)


def test_fetch_block_timestamps_raises_after_persistent_rate_limit():
    """Rate limiting on every attempt exhausts the retry budget and surfaces a RateLimitError."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [http_429() for _ in range(chain.RATE_LIMIT_ATTEMPTS)]

    with patch("ad_voting_metrics.sources.chain.time.sleep"), pytest.raises(chain.RateLimitError, match="single-block"):
        chain.fetch_block_timestamps(mock_w3, {5}, {})

    assert batch.execute.call_count == chain.RATE_LIMIT_ATTEMPTS


def test_fetch_block_timestamps_halves_batch_size_on_rate_limit():
    """A rate-limited batch is halved and retried after a short pause, and the smaller size carries over."""
    total = chain.TIMESTAMP_BATCH_SIZE
    half = total // 2
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [
        http_429(),
        [{"timestamp": i} for i in range(half)],
        [{"timestamp": i} for i in range(half, total)],
    ]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, set(range(total)), timestamps)

    assert timestamps == {i: i for i in range(total)}
    assert batch.execute.call_count == 3
    mock_sleep.assert_called_once_with(chain.RATE_LIMIT_BASE_DELAY_SECONDS)


def test_fetch_block_timestamps_backs_off_at_single_block_batches():
    """Once batches are one block, rate limits back off exponentially, and a success resets the schedule."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [
        http_429(),  # 2-block batch: halve to 1
        http_429(),  # 1-block: attempt 1
        http_429(),  # 1-block: attempt 2
        [{"timestamp": 1}],
        http_429(),  # 1-block: schedule restarts after the success
        [{"timestamp": 2}],
    ]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, {5, 6}, timestamps)

    assert timestamps == {5: 1, 6: 2}
    base = chain.RATE_LIMIT_BASE_DELAY_SECONDS
    assert [c.args[0] for c in mock_sleep.call_args_list] == [base, base, base * 2, base]


def test_fetch_block_timestamps_retries_sequential_call_after_rpc_rate_limit():
    """A JSON-RPC "Too Many Requests" error on the sequential fallback is retried, not treated as a node failure."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.side_effect = ValueError("batch not supported")
    rate_limited = Web3RPCError(
        "Too Many Requests",
        rpc_response={
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": chain.RATE_LIMIT_RPC_CODE, "message": "Too Many Requests"},
        },
    )
    mock_w3.eth.get_block.side_effect = [rate_limited, {"timestamp": 9}]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, {5}, timestamps)

    assert timestamps == {5: 9}
    mock_sleep.assert_called_once_with(chain.RATE_LIMIT_BASE_DELAY_SECONDS)


def _infura_too_many_requests() -> BadResponseFormat:
    """Build the BadResponseFormat web3 raises when Infura answers a batch item with its bare rate-limit object."""
    raw = {"code": chain.RATE_LIMIT_RPC_CODE, "message": "Too Many Requests", "data": {"see": "https://infura.io"}}
    return BadResponseFormat(f"The response was in an unexpected format. The raw response is: {raw}")


def test_fetch_block_timestamps_retries_batch_after_in_body_rate_limit():
    """Infura's HTTP 200 rate-limit error inside the batch is retried like an HTTP 429."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [_infura_too_many_requests(), [{"timestamp": 7}]]
    timestamps: dict[int, int] = {}

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep:
        chain.fetch_block_timestamps(mock_w3, {5}, timestamps)

    assert timestamps == {5: 7}
    mock_sleep.assert_called_once_with(chain.RATE_LIMIT_BASE_DELAY_SECONDS)


def test_fetch_block_timestamps_raises_other_bad_response_formats_immediately():
    """A malformed batch response that is not a rate limit is not retried."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = BadResponseFormat("The raw response is: {'code': -32600, 'message': 'Invalid Request'}")

    with patch("ad_voting_metrics.sources.chain.time.sleep") as mock_sleep, pytest.raises(BadResponseFormat):
        chain.fetch_block_timestamps(mock_w3, {5}, {})

    mock_sleep.assert_not_called()
    assert batch.execute.call_count == 1


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
