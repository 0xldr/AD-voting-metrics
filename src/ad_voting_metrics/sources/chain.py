"""web3 plumbing shared by the modules that read mainnet: finality, block dating, batched fetches, backoff."""

import logging
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from http import HTTPStatus
from itertools import batched
from typing import Any

from requests.exceptions import HTTPError
from web3 import Web3
from web3.exceptions import Web3RPCError
from web3.types import BlockData

logger = logging.getLogger(__name__)

# Reorg safety: every on-chain read stops this many blocks behind the head.
FINALITY_BLOCKS = 12

# get_block calls per JSON-RPC batch, and the exponential backoff schedule applied when the provider rate-limits a
# batch (HTTP 429).
TIMESTAMP_BATCH_SIZE = 100
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_SECONDS = 1.0


def safe_head(w3: Web3) -> int:
    """Return the newest block number this project treats as final."""
    return w3.eth.block_number - FINALITY_BLOCKS


def block_timestamp(block: BlockData) -> int:
    """Return a mined block's UNIX timestamp.

    web3 marks `timestamp` optional on BlockData because pending blocks may omit it; every block this project reads is
    mined, so the key is always present.
    """
    return block["timestamp"]


def utc_date(timestamp: int) -> date:
    """Return the UTC calendar day of a UNIX timestamp."""
    return datetime.fromtimestamp(timestamp, tz=UTC).date()


def _get_blocks_batched(w3: Web3, block_nums: Sequence[int]) -> list[Any]:
    """Fetch full blocks for block_nums in one JSON-RPC batch.

    A rate-limited batch (HTTP 429) is retried with exponential backoff, up to RATE_LIMIT_ATTEMPTS attempts total.
    Providers that reject batch requests outright raise Web3RPCError or ValueError, which callers handle.

    Raises:
        HTTPError: if the provider still rate-limits on the final attempt, or returns any other HTTP error.
    """
    attempt = 0
    while True:
        try:
            with w3.batch_requests() as batch:
                for block_num in block_nums:
                    batch.add(w3.eth.get_block(block_num))
                return batch.execute()
        except HTTPError as e:
            attempt += 1
            rate_limited = e.response is not None and e.response.status_code == HTTPStatus.TOO_MANY_REQUESTS
            if not rate_limited or attempt == RATE_LIMIT_ATTEMPTS:
                raise
            delay = RATE_LIMIT_BASE_DELAY_SECONDS * 2 ** (attempt - 1)
            logger.info("Provider rate-limited a %d-block batch; retrying in %.0fs", len(block_nums), delay)
            time.sleep(delay)


def fetch_block_timestamps(w3: Web3, blocks: set[int], block_timestamps: dict[int, int]) -> None:
    """Extend block_timestamps in place with UNIX timestamps for any blocks it lacks.

    Missing blocks are requested in JSON-RPC batches of at most TIMESTAMP_BATCH_SIZE, with backoff on rate limits;
    providers that reject batch requests fall back to one get_block call per block.

    Raises:
        RuntimeError: if a sequential get_block call fails.
    """
    missing = sorted(blocks - block_timestamps.keys())

    for chunk in batched(missing, TIMESTAMP_BATCH_SIZE, strict=False):
        try:
            blocks_data = _get_blocks_batched(w3, chunk)
        except (Web3RPCError, ValueError) as e:
            logger.debug("Batched get_block failed (%s); falling back to sequential fetches", e)
            for block_num in chunk:
                try:
                    fetched_block = w3.eth.get_block(block_num)
                except Web3RPCError as seq_error:
                    msg = f"get_block({block_num}) failed: {seq_error}"
                    raise RuntimeError(msg) from seq_error
                block_timestamps[block_num] = block_timestamp(fetched_block)
        else:
            for block_num, batched_block in zip(chunk, blocks_data, strict=True):
                block_timestamps[block_num] = block_timestamp(batched_block)
