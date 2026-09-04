"""On-chain delegation: Lock/Free event replay for daily SKY delegation totals.

Public entry points:
  - get_all_sky_delegated: fetch Lock/Free events, return the running-total balance on each event day
  - get_delegate_list_sky: forward-fill those balances onto a (delegate, day) grid for a period, zero before first event
  - build_sky_lookup: materialize per-day balance DataFrame into an O(1) (contract, date) dict
  - DelegationCache: the synced events and block timestamps, persisted as JSON so steady-state runs only fetch new
    blocks since the V3 VoteDelegateFactory (block 22368737)
"""

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast

import pandas as pd
from requests.exceptions import HTTPError
from web3 import Web3
from web3.exceptions import Web3RPCError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.sources.json_cache import load_json_cache, save_json_cache

logger = logging.getLogger(__name__)

# V3 VoteDelegateFactory deployment block (fromBlock floor for all V3 contracts).
V3_FACTORY_BLOCK = 22368737

# Event topic hashes (keccak256 of "Lock(address,uint256)" and "Free(address,uint256)").
# Raw eth_getLogs with hand-built topics (rather than ABI-based contract.events.X.get_logs,
# as sky_executive_onchain uses) because one raw call filters across every delegate contract
# at once; ContractEvent.get_logs binds to a single contract address.
LOCK_TOPIC = Web3.to_hex(Web3.keccak(text="Lock(address,uint256)"))
FREE_TOPIC = Web3.to_hex(Web3.keccak(text="Free(address,uint256)"))

# Reorg safety: only sync up to (block_number - this many).
FINALITY_BLOCKS = 12

# Adaptive getLogs chunking: start here, halve on range error down to MIN.
INITIAL_CHUNK_BLOCKS = 100_000
MIN_CHUNK_BLOCKS = 2_000

# Block-timestamp fetching: get_block calls per JSON-RPC batch, and the exponential
# backoff schedule applied when the provider rate-limits a batch (HTTP 429).
TIMESTAMP_BATCH_SIZE = 100
RATE_LIMIT_ATTEMPTS = 5
RATE_LIMIT_BASE_DELAY_SECONDS = 1.0

# A Lock/Free event: (block number, signed wad). Lock amounts are positive, Free amounts negative.
type Event = tuple[int, int]

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


@dataclass
class DelegationCache:
    """Synced Lock/Free events and the block timestamps needed to date them.

    `events` maps a lowercased contract address to its events in sync order. `block_timestamps` maps a block number to
    its UNIX timestamp. `last_synced_block` is None until the first sync completes.

    On disk this is JSON, where object keys are strings and older files stored wads as strings; `load` normalises both
    to ints so the rest of the module works with native types.
    """

    last_synced_block: int | None = None
    events: dict[str, list[Event]] = field(default_factory=dict)
    block_timestamps: dict[int, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> DelegationCache:
        """Read the cache from disk; an absent file yields an empty cache.

        Returns:
            The parsed cache.
        """
        data = load_json_cache(path)
        return cls(
            last_synced_block=data.get("last_synced_block"),
            events={
                contract.lower(): [(int(block), int(wad)) for block, wad in events]
                for contract, events in data.get("events", {}).items()
            },
            block_timestamps={int(block): ts for block, ts in data.get("block_timestamps", {}).items()},
        )

    def save(self, path: Path) -> None:
        """Persist the cache atomically; json turns the int block keys into strings."""
        save_json_cache(asdict(self), path)


# ---------------------------------------------------------------------------
# Event sync
# ---------------------------------------------------------------------------


def _fetch_event_logs(
    w3: Web3,
    contracts: list[str],
    from_block: int,
    safe_head: int,
) -> tuple[dict[str, list[Event]], set[int]]:
    """Fetch Lock/Free logs across a block range using adaptive chunking.

    Both event types are fetched in a single eth_getLogs call per chunk (an OR-filter on topic0) and signed by topic
    afterwards. Chunks start at INITIAL_CHUNK_BLOCKS; on an eth_getLogs range/size error, the window halves and
    retries down to MIN_CHUNK_BLOCKS. A size that worked carries over to subsequent chunks so a provider with a low
    limit isn't re-probed on every window.

    Returns a tuple of (events_by_contract, new_blocks). events_by_contract maps a lowercased contract address to its
    events; new_blocks is the set of block numbers in which an event was seen.

    Raises:
        RuntimeError: if eth_getLogs fails even at MIN_CHUNK_BLOCKS.
    """
    contracts_checksummed = [Web3.to_checksum_address(c) for c in contracts]
    events_by_contract: dict[str, list[Event]] = {}
    new_blocks: set[int] = set()

    block = from_block
    chunk_size = INITIAL_CHUNK_BLOCKS
    while block <= safe_head:
        while True:
            to_block = min(block + chunk_size - 1, safe_head)
            try:
                logs = w3.eth.get_logs(
                    {
                        "fromBlock": block,
                        "toBlock": to_block,
                        "address": contracts_checksummed,
                        "topics": [[LOCK_TOPIC, FREE_TOPIC]],
                    }
                )
            except Web3RPCError as e:
                if chunk_size <= MIN_CHUNK_BLOCKS:
                    msg = f"eth_getLogs failed even at the minimum chunk size of {MIN_CHUNK_BLOCKS} blocks: {e}"
                    raise RuntimeError(msg) from e
                # Clamp to MIN so halving can't step past it and loop forever.
                chunk_size = max(chunk_size // 2, MIN_CHUNK_BLOCKS)
                logger.debug(
                    "getLogs range error on blocks %d-%d; halving to %d and retrying",
                    block,
                    to_block,
                    chunk_size,
                )
                continue

            for log in logs:
                contract_lower = log["address"].lower()
                wad = Web3.to_int(log["data"])
                if Web3.to_hex(log["topics"][0]) == FREE_TOPIC:
                    wad = -wad
                events_by_contract.setdefault(contract_lower, []).append((log["blockNumber"], wad))
                new_blocks.add(log["blockNumber"])

            block = to_block + 1
            break

    return events_by_contract, new_blocks


def _get_blocks_batched(w3: Web3, block_nums: list[int]) -> list[Any]:
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


def _fetch_block_timestamps(
    w3: Web3,
    blocks: set[int],
    block_timestamps: dict[int, int],
) -> dict[int, int]:
    """Fetch UNIX timestamps for any blocks not already in block_timestamps.

    Missing blocks are requested in JSON-RPC batches of at most TIMESTAMP_BATCH_SIZE, with backoff on rate limits;
    providers that reject batch requests fall back to one get_block call per block.

    Returns the block_timestamps dict, extended with newly fetched entries.

    Raises:
        RuntimeError: if a sequential get_block call fails.
    """
    missing = sorted(blocks - block_timestamps.keys())

    for start in range(0, len(missing), TIMESTAMP_BATCH_SIZE):
        chunk = missing[start : start + TIMESTAMP_BATCH_SIZE]
        try:
            blocks_data = _get_blocks_batched(w3, chunk)
        except (Web3RPCError, ValueError) as e:
            logger.debug("Batched get_block failed (%s); falling back to sequential fetches", e)
            for block_num in chunk:
                try:
                    fetched_block = cast("dict[str, Any]", w3.eth.get_block(block_num))
                except Web3RPCError as seq_error:
                    msg = f"get_block({block_num}) failed: {seq_error}"
                    raise RuntimeError(msg) from seq_error
                block_timestamps[block_num] = fetched_block["timestamp"]
        else:
            for block_num, batched_block in zip(chunk, blocks_data, strict=True):
                # `timestamp` is NotRequired on BlockData per web3-stubs, but full
                # blocks always carry it at runtime; widen to plain dict for access.
                block_timestamps[block_num] = cast("dict[str, Any]", batched_block)["timestamp"]

    return block_timestamps


def _sync_events(
    w3: Web3,
    contracts: list[str],
    *,
    cache_path: Path,
    rebuild: bool = False,
) -> DelegationCache:
    """Fetch Lock/Free events from the chain, update the cache on disk, return it.

    Loads the cache; if rebuild=True, discards prior events and syncs from V3_FACTORY_BLOCK.
    Otherwise syncs from (last_synced_block + 1).
    """
    cache = DelegationCache() if rebuild else DelegationCache.load(cache_path)
    for contract in contracts:
        cache.events.setdefault(contract.lower(), [])

    from_block = V3_FACTORY_BLOCK
    if cache.last_synced_block is not None:
        from_block = max(V3_FACTORY_BLOCK, cache.last_synced_block + 1)

    safe_head = w3.eth.block_number - FINALITY_BLOCKS
    if from_block > safe_head:
        logger.info("Already synced up to block %d; nothing new to fetch", cache.last_synced_block)
        return cache

    logger.info(
        "Syncing Lock/Free events from block %d to %d (%d blocks, %d contracts)",
        from_block,
        safe_head,
        safe_head - from_block + 1,
        len(contracts),
    )

    events_by_contract, new_blocks = _fetch_event_logs(w3, contracts, from_block, safe_head)
    for contract_lower, events in events_by_contract.items():
        cache.events.setdefault(contract_lower, []).extend(events)

    cache.block_timestamps = _fetch_block_timestamps(w3, new_blocks, cache.block_timestamps)
    cache.last_synced_block = safe_head
    cache.save(cache_path)

    logger.info(
        "Synced %d new events across %d blocks; last_synced_block=%d",
        sum(len(events) for events in events_by_contract.values()),
        len(new_blocks),
        safe_head,
    )
    return cache


# ---------------------------------------------------------------------------
# Daily series building
# ---------------------------------------------------------------------------


def _contract_cumulative_balances(
    events: list[Event],
    block_timestamps: dict[int, int],
    contract: str,
) -> dict[date, int]:
    """Aggregate one contract's Lock/Free events into a cumulative daily balance (wei).

    Events lacking a cached block timestamp are skipped with a warning.

    Returns a mapping of event date to the running-total balance (wei) as of that date. Empty if the contract has no
    timestamped events.

    Raises:
        ValueError: if the running total goes negative, indicating missing or
            misattributed events.
    """
    events_by_date: dict[date, int] = {}
    for block, wad in events:
        if block not in block_timestamps:
            logger.warning("Block %d has no cached timestamp; skipping event", block)
            continue
        event_date = datetime.fromtimestamp(block_timestamps[block], tz=UTC).date()
        events_by_date[event_date] = events_by_date.get(event_date, 0) + wad

    cumulative_by_date: dict[date, int] = {}
    running_total = 0
    for event_date in sorted(events_by_date):
        running_total += events_by_date[event_date]
        if running_total < 0:
            msg = (
                f"Negative running total on contract {contract} at {event_date}: {running_total} wei. "
                "This indicates missing or misattributed events."
            )
            raise ValueError(msg)
        cumulative_by_date[event_date] = running_total
    return cumulative_by_date


def _build_balance_series(cache: DelegationCache) -> pd.DataFrame:
    """Build each contract's running-total SKY balance on every day it changed.

    Days without an event are absent; `get_delegate_list_sky` forward-fills onto the period's daily grid.

    Returns a DataFrame indexed on (delegation_contract, dt) with a running_total_balance column in SKY.
    """
    rows = [
        {
            "delegation_contract": contract,
            "dt": event_date,
            "running_total_balance": float(Web3.from_wei(wei, "ether")),
        }
        for contract, events in cache.events.items()
        for event_date, wei in _contract_cumulative_balances(events, cache.block_timestamps, contract).items()
    ]
    columns = ["delegation_contract", "dt", "running_total_balance"]
    return pd.DataFrame(rows, columns=columns).set_index(["delegation_contract", "dt"])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_sky_delegated(
    contracts: list[str],
    *,
    w3: Web3,
    cache_path: Path,
    rebuild: bool = False,
) -> pd.DataFrame:
    """Fetch SKY delegation balances from on-chain Lock/Free events.

    Replays events from the V3 VoteDelegateFactory block, caching them to disk for incremental syncs. Returns one
    running_total_balance row per (delegation_contract, event day).

    Args:
        contracts: list of delegate contract addresses (any case).
        w3: connected Web3 client.
        cache_path: JSON file holding the synced events and block timestamps.
        rebuild: if True, resyncs from V3_FACTORY_BLOCK, discarding cache.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with running_total_balance column.
        delegation_contract is lowercased; dt is datetime.date.

    Raises:
        RuntimeError: if the sync fails even at the minimum getLogs chunk size.
    """
    logger.info("Syncing delegation events%s...", " (rebuild)" if rebuild else "")
    cache = _sync_events(w3, contracts, cache_path=cache_path, rebuild=rebuild)

    event_count = sum(len(events) for events in cache.events.values())
    logger.info("Building running-total balances from %d events", event_count)
    df = _build_balance_series(cache)
    logger.info(
        "%d balance changes across %d contracts",
        len(df),
        len(df.index.get_level_values(0).unique()),
    )

    return df


def get_delegate_list_sky(
    delegates: list[Delegate],
    period: MonthPeriod,
    *,
    w3: Web3,
    cache_path: Path,
    rebuild: bool = False,
) -> pd.DataFrame:
    """Build one row per (delegate, day) with SKY balance for the period.

    Each day reflects the delegate's most recent on-chain balance, carried forward: a delegate
    whose last Lock/Free predates the period still holds that balance every day. Days before a
    delegate's first-ever Lock/Free event (no balance yet) are zero.

    Args:
        delegates: roster entries active during the period.
        period: MonthPeriod to cover.
        w3: connected Web3 client.
        cache_path: JSON file holding the synced events and block timestamps.
        rebuild: if True, forces a full re-sync from V3_FACTORY_BLOCK.

    Returns:
        DataFrame with columns: contract, name, date, sky. One row per (delegate, day)
        covering every day in the period.
    """
    contracts = [d.vote_delegate_address for d in delegates]
    names_by_contract = {d.vote_delegate_address: d.name for d in delegates}
    all_sky_delegated = get_all_sky_delegated(contracts, w3=w3, cache_path=cache_path, rebuild=rebuild)

    days = list(pd.date_range(period.start, period.end, freq="D").date)

    # Forward-fill over event days plus period days so a pre-period balance carries in, then
    # restrict to the period; days before a contract's first event stay 0.
    balances = all_sky_delegated["running_total_balance"]
    all_days = sorted(set(balances.index.get_level_values("dt")) | set(days))
    full_index = pd.MultiIndex.from_product([contracts, all_days], names=["delegation_contract", "dt"])
    carried = balances.reindex(full_index).groupby(level="delegation_contract").ffill()

    target = pd.MultiIndex.from_product([contracts, days], names=["delegation_contract", "dt"])
    filled = carried.reindex(target).fillna(0.0).astype(float).reset_index()

    return pd.DataFrame(
        {
            "contract": filled["delegation_contract"],
            "name": filled["delegation_contract"].map(names_by_contract),
            "date": filled["dt"],
            "sky": filled["running_total_balance"],
        }
    )


def build_sky_lookup(df_sky: pd.DataFrame) -> dict[tuple[str, date], float]:
    """Materialize df_sky into a (contract, date) -> sky-balance dict for O(1) lookup.

    Returns:
        Mapping of (contract, date) to that day's SKY balance.
    """
    lookup = df_sky.set_index(["contract", "date"])["sky"].astype(float).to_dict()
    return cast("dict[tuple[str, date], float]", lookup)
