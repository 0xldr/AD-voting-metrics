"""On-chain delegation: Lock/Free event replay for daily SKY delegation totals.

Lock/Free events from every delegate contract are synced into a JSON cache and replayed into a running balance per
contract, then carried forward onto each day of the period. Steady-state runs fetch only the blocks since the last
sync; a contract new to the roster is backfilled from the factory block on its first appearance.
"""

import logging
from bisect import bisect_right
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd
from web3 import Web3
from web3.exceptions import Web3RPCError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.sources.chain import FINALITY_BLOCKS, fetch_block_timestamps, safe_head, utc_date
from ad_voting_metrics.sources.json_cache import load_json_cache, save_json_cache

logger = logging.getLogger(__name__)

__all__ = ["FINALITY_BLOCKS", "V3_FACTORY_BLOCK", "DelegationCache", "daily_balances", "sync_events"]

# V3 VoteDelegateFactory deployment block (fromBlock floor for all V3 contracts).
V3_FACTORY_BLOCK = 22368737

# Event topic hashes (keccak256 of "Lock(address,uint256)" and "Free(address,uint256)").
# Raw eth_getLogs with hand-built topics (rather than ABI-based contract.events.X.get_logs,
# as sky_executive uses) because one raw call filters across every delegate contract
# at once; ContractEvent.get_logs binds to a single contract address.
LOCK_TOPIC = Web3.to_hex(Web3.keccak(text="Lock(address,uint256)"))
FREE_TOPIC = Web3.to_hex(Web3.keccak(text="Free(address,uint256)"))

# Adaptive getLogs chunking: start here, halve on range error down to MIN.
INITIAL_CHUNK_BLOCKS = 100_000
MIN_CHUNK_BLOCKS = 2_000

# A Lock/Free event: (block number, signed wad). Lock amounts are positive, Free amounts negative.
type Event = tuple[int, int]


@dataclass
class DelegationCache:
    """Synced Lock/Free events and the block timestamps needed to date them.

    `events` maps a lowercased contract address to its events; a key with an empty list means the contract has been
    synced and simply has no events yet. `block_timestamps` maps a block number to its UNIX timestamp.
    `last_synced_block` is None until the first sync completes.

    On disk this is JSON, where object keys are strings and older files stored wads as strings; `load` normalises both
    to ints so the rest of the module works with native types.
    """

    last_synced_block: int | None = None
    events: dict[str, list[Event]] = field(default_factory=dict)
    block_timestamps: dict[int, int] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> DelegationCache:
        """Read the cache from disk; an absent file yields an empty cache."""
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
            chunk_size = max(chunk_size // 2, MIN_CHUNK_BLOCKS)
            logger.debug("getLogs range error on blocks %d-%d; halving to %d and retrying", block, to_block, chunk_size)
            continue

        for log in logs:
            contract_lower = log["address"].lower()
            wad = Web3.to_int(log["data"])
            if Web3.to_hex(log["topics"][0]) == FREE_TOPIC:
                wad = -wad
            events_by_contract.setdefault(contract_lower, []).append((log["blockNumber"], wad))
            new_blocks.add(log["blockNumber"])
        block = to_block + 1

    return events_by_contract, new_blocks


def sync_events(w3: Web3, contracts: list[str], *, cache_path: Path, rebuild: bool) -> DelegationCache:
    """Fetch Lock/Free events for the contracts from the chain, update the cache on disk, and return it.

    With rebuild the prior cache is discarded and every contract syncs from V3_FACTORY_BLOCK. Otherwise contracts the
    cache already knows resume from the block after last_synced_block, and contracts new to the roster are first
    backfilled from V3_FACTORY_BLOCK up to last_synced_block so their full history is present. Only blocks at least
    FINALITY_BLOCKS behind the head are synced.

    Raises:
        RuntimeError: if the sync fails even at the minimum getLogs chunk size.
    """
    logger.info("Syncing delegation events%s...", " (rebuild)" if rebuild else "")
    cache = DelegationCache() if rebuild else DelegationCache.load(cache_path)
    contracts = [c.lower() for c in contracts]
    head = safe_head(w3)

    # Each range is (contracts, first block, last block); the backfill for newcomers runs before the shared tail.
    ranges: list[tuple[list[str], int, int]] = []
    if cache.last_synced_block is None:
        from_block = V3_FACTORY_BLOCK
    else:
        new_contracts = [c for c in contracts if c not in cache.events]
        if new_contracts:
            ranges.append((new_contracts, V3_FACTORY_BLOCK, cache.last_synced_block))
        from_block = cache.last_synced_block + 1
    if from_block <= head:
        ranges.append((contracts, from_block, head))

    for contract in contracts:
        cache.events.setdefault(contract, [])

    if not ranges:
        logger.info("Already synced up to block %d; nothing new to fetch", cache.last_synced_block)
        return cache

    new_blocks: set[int] = set()
    event_count = 0
    for range_contracts, first, last in ranges:
        logger.info(
            "Syncing Lock/Free events from block %d to %d (%d blocks, %d contracts)",
            first,
            last,
            last - first + 1,
            len(range_contracts),
        )
        events_by_contract, blocks = _fetch_event_logs(w3, range_contracts, first, last)
        for contract, events in events_by_contract.items():
            cache.events[contract].extend(events)
            event_count += len(events)
        new_blocks |= blocks

    fetch_block_timestamps(w3, new_blocks, cache.block_timestamps)
    cache.last_synced_block = max(head, cache.last_synced_block or 0)
    cache.save(cache_path)

    logger.info("Synced %d new events across %d blocks; last_synced_block=%d", event_count, len(new_blocks), head)
    return cache


def _contract_cumulative_balances(
    events: list[Event],
    block_timestamps: dict[int, int],
    contract: str,
) -> dict[date, int]:
    """Aggregate one contract's Lock/Free events into a cumulative balance (wei) on each day one occurred.

    Empty if the contract has no events.

    Raises:
        ValueError: if an event's block has no cached timestamp (the cache is inconsistent and needs --rebuild), or if
            the running total goes negative, indicating missing or misattributed events.
    """
    events_by_date: dict[date, int] = {}
    for block, wad in events:
        if block not in block_timestamps:
            msg = f"Block {block} of contract {contract} has no cached timestamp; the cache is inconsistent (--rebuild)"
            raise ValueError(msg)
        event_date = utc_date(block_timestamps[block])
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


def daily_balances(cache: DelegationCache, delegates: list[Delegate], period: MonthPeriod) -> pd.DataFrame:
    """Build one row per (delegate, day) across the period with columns contract, name, date, sky, rank.

    Each day carries the delegate's balance as of their latest Lock/Free on or before it, so a balance set before the
    period holds on every day of it. Days before a delegate's first event are zero. Within each day delegates are
    ranked by balance (1 = most SKY), ties broken by roster order so no two share a rank, and the frame is sorted by
    date then rank.
    """
    days = pd.date_range(period.start, period.end, freq="D").date
    rows = []
    for delegate in delegates:
        contract = delegate.vote_delegate_address
        balances = _contract_cumulative_balances(cache.events.get(contract, []), cache.block_timestamps, contract)
        event_days = sorted(balances)
        for day in days:
            events_so_far = bisect_right(event_days, day)
            wei = balances[event_days[events_so_far - 1]] if events_so_far else 0
            rows.append(
                {"contract": contract, "name": delegate.name, "date": day, "sky": float(Web3.from_wei(wei, "ether"))}
            )
    daily = pd.DataFrame(rows, columns=["contract", "name", "date", "sky"])
    rank = daily.groupby("date")["sky"].rank(method="first", ascending=False).astype(int)
    return daily.assign(rank=rank).sort_values(["date", "rank"]).reset_index(drop=True)
