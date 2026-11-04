"""web3 plumbing shared by the modules that read mainnet: finality, block dating, batched fetches, backoff."""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from functools import partial
from http import HTTPStatus
from typing import Any

from requests.exceptions import HTTPError
from web3 import Web3
from web3.exceptions import BadResponseFormat, Web3RPCError
from web3.types import BlockData

logger = logging.getLogger(__name__)

# Reorg safety: every on-chain read stops this many blocks behind the head.
FINALITY_BLOCKS = 12

# get_block calls per JSON-RPC batch: start here and halve on each rate limit, down to a single block. Providers
# that meter credits per second bill every call in a batch at once, so a batch can be too large to ever succeed.
TIMESTAMP_BATCH_SIZE = 100

# Exponential backoff schedule applied when the provider rate-limits any RPC call.
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_SECONDS = 1.0

# JSON-RPC error code Infura uses for "Too Many Requests". On a single request it is a normal JSON-RPC error; inside
# a batch it arrives as a bare {"code": -32005, "message": ...} object, which web3 cannot parse and surfaces as
# BadResponseFormat.
RATE_LIMIT_RPC_CODE = -32005


class RateLimitError(RuntimeError):
    """The provider kept rate-limiting a call for every attempt in the backoff schedule."""


def safe_head(w3: Web3) -> int:
    """Return the newest block number this project treats as final."""
    return w3.eth.block_number - FINALITY_BLOCKS


def block_timestamp(block: BlockData) -> int:
    """Return a mined block's UNIX timestamp.

    Raises:
        ValueError: if the block has no timestamp, which web3 allows for pending blocks only.
    """
    timestamp = block.get("timestamp")
    if timestamp is None:
        msg = f"block {block.get('number')} has no timestamp; only pending blocks omit it"
        raise ValueError(msg)
    return timestamp


def utc_date(timestamp: int) -> date:
    """Return the UTC calendar day of a UNIX timestamp."""
    return datetime.fromtimestamp(timestamp, tz=UTC).date()


def _is_rate_limited(error: Exception) -> bool:
    """Return whether a failed call was rate-limited: an HTTP 429, or RATE_LIMIT_RPC_CODE in any form web3 raises it."""
    if isinstance(error, HTTPError):
        return error.response is not None and error.response.status_code == HTTPStatus.TOO_MANY_REQUESTS
    if isinstance(error, Web3RPCError):
        rpc_error = (error.rpc_response or {}).get("error")
        return isinstance(rpc_error, dict) and rpc_error.get("code") == RATE_LIMIT_RPC_CODE
    return isinstance(error, BadResponseFormat) and f"'code': {RATE_LIMIT_RPC_CODE}" in str(error)


def retry_on_rate_limit[T](call: Callable[[], T], description: str) -> T:
    """Run call, retrying with exponential backoff while the provider rate-limits it.

    Up to RATE_LIMIT_ATTEMPTS attempts are made; `description` names the call in the retry log line. Any failure other
    than a rate limit propagates unchanged on the first occurrence.

    Raises:
        RateLimitError: if the provider still rate-limits the call on the final attempt.
    """
    attempt = 0
    while True:
        try:
            return call()
        except (HTTPError, Web3RPCError, BadResponseFormat) as e:
            if not _is_rate_limited(e):
                raise
            attempt += 1
            if attempt == RATE_LIMIT_ATTEMPTS:
                msg = f"Provider still rate-limited {description} after {RATE_LIMIT_ATTEMPTS} attempts"
                raise RateLimitError(msg) from e
            delay = RATE_LIMIT_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            logger.info("Provider rate-limited %s; retrying in %.0fs", description, delay)
            time.sleep(delay)


def _get_blocks_batched(w3: Web3, block_nums: Sequence[int]) -> list[Any]:
    """Fetch full blocks for block_nums in one JSON-RPC batch; a single attempt with no retry."""
    with w3.batch_requests() as batch:
        for block_num in block_nums:
            batch.add(w3.eth.get_block(block_num))
        return batch.execute()


def _fetch_timestamps_batched(w3: Web3, missing: Sequence[int], block_timestamps: dict[int, int]) -> None:
    """Fill block_timestamps for `missing` from JSON-RPC batches whose size adapts to the provider's rate limit.

    Batches start at TIMESTAMP_BATCH_SIZE. Each rate limit halves the batch, waits RATE_LIMIT_BASE_DELAY_SECONDS and
    retries the same blocks; a size that worked carries over to subsequent batches. Once a batch is a single block
    there is nothing left to shrink, so the exponential backoff schedule applies instead, up to RATE_LIMIT_ATTEMPTS
    consecutive rate limits. Providers that reject batch requests outright raise Web3RPCError or ValueError, which
    the caller handles.

    Raises:
        RateLimitError: if the provider still rate-limits a single-block batch on the final attempt.
    """
    batch_size = TIMESTAMP_BATCH_SIZE
    start = 0
    single_block_attempts = 0
    while start < len(missing):
        chunk = missing[start : start + batch_size]
        try:
            blocks_data = _get_blocks_batched(w3, chunk)
        except (HTTPError, Web3RPCError, BadResponseFormat) as e:
            if not _is_rate_limited(e):
                raise
            if len(chunk) > 1:
                batch_size = len(chunk) // 2
                delay = RATE_LIMIT_BASE_DELAY_SECONDS
                logger.info(
                    "Provider rate-limited a %d-block batch; retrying with %d blocks in %.0fs",
                    len(chunk),
                    batch_size,
                    delay,
                )
            else:
                single_block_attempts += 1
                if single_block_attempts == RATE_LIMIT_ATTEMPTS:
                    msg = f"Provider still rate-limited a single-block batch after {RATE_LIMIT_ATTEMPTS} attempts"
                    raise RateLimitError(msg) from e
                delay = RATE_LIMIT_BASE_DELAY_SECONDS * 2 ** (single_block_attempts - 1)
                logger.info("Provider rate-limited a single-block batch; retrying in %.0fs", delay)
            time.sleep(delay)
            continue

        single_block_attempts = 0
        for block_num, batched_block in zip(chunk, blocks_data, strict=True):
            block_timestamps[block_num] = block_timestamp(batched_block)
        start += len(chunk)


def _fetch_timestamps_sequential(w3: Web3, missing: Sequence[int], block_timestamps: dict[int, int]) -> None:
    """Fill block_timestamps for `missing` with one get_block call per block, with backoff on rate limits.

    Raises:
        RuntimeError: if a get_block call fails.
        RateLimitError: if the provider still rate-limits a call on the final attempt.
    """
    for block_num in missing:
        try:
            fetched_block = retry_on_rate_limit(partial(w3.eth.get_block, block_num), f"get_block({block_num})")
        except Web3RPCError as e:
            msg = f"get_block({block_num}) failed: {e}"
            raise RuntimeError(msg) from e
        block_timestamps[block_num] = block_timestamp(fetched_block)


def fetch_block_timestamps(w3: Web3, blocks: set[int], block_timestamps: dict[int, int]) -> None:
    """Extend block_timestamps in place with UNIX timestamps for any blocks it lacks.

    Missing blocks are requested in JSON-RPC batches sized to the provider's rate limit; providers that reject batch
    requests fall back to one get_block call per block. Both paths back off on rate limits.

    Raises:
        RuntimeError: if a sequential get_block call fails.
        RateLimitError: if the provider still rate-limits a call on the final attempt.
    """
    missing = sorted(blocks - block_timestamps.keys())
    try:
        _fetch_timestamps_batched(w3, missing, block_timestamps)
    except (Web3RPCError, ValueError) as e:
        logger.debug("Batched get_block failed (%s); falling back to sequential fetches", e)
        still_missing = [block_num for block_num in missing if block_num not in block_timestamps]
        _fetch_timestamps_sequential(w3, still_missing, block_timestamps)
