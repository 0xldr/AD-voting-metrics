"""On-chain delegation: Lock/Free event replay for daily SKY delegation totals.

Public entry points:
  - get_all_sky_delegated: fetch Lock/Free events, build daily running totals, return indexed DataFrame
  - get_delegate_list_sky: project daily totals onto (delegate, day) grid for a period, zero-filling missing days
  - build_sky_lookup: materialize per-day balance DataFrame into an O(1) (contract, date) dict

Events are synced incrementally from the V3 VoteDelegateFactory (block 22368737) and cached to
output_data/delegation_cache.json, so steady-state runs only fetch new blocks.
"""

import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
from web3 import Web3
from web3.exceptions import Web3RPCError

from ad_voting_metrics.period import MonthPeriod

logger = logging.getLogger(__name__)

# V3 VoteDelegateFactory deployment block (fromBlock floor for all V3 contracts).
V3_FACTORY_BLOCK = 22368737

# Event topic hashes (keccak256 of "Lock(address,uint256)" and "Free(address,uint256)").
LOCK_TOPIC = Web3.keccak(text="Lock(address,uint256)").hex()
FREE_TOPIC = Web3.keccak(text="Free(address,uint256)").hex()

# Reorg safety: only sync up to (block_number - this many).
FINALITY_BLOCKS = 12

# Adaptive getLogs chunking: start here, halve on range error down to MIN.
INITIAL_CHUNK_BLOCKS = 100_000
MIN_CHUNK_BLOCKS = 2_000

# Cache location.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_CACHE_PATH = REPO_ROOT / "output_data" / "delegation_cache.json"


# ---------------------------------------------------------------------------
# Cache load/save
# ---------------------------------------------------------------------------


def _load_cache(path: Path) -> dict[str, Any]:
    """Load the delegation cache from disk.

    Returns:
        The cache dict with keys: last_synced_block, events, block_timestamps.
        Empty dict if the file doesn't exist.
    """
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    # Normalize contract addresses to lowercase in the events dict.
    if "events" in data:
        data["events"] = {contract.lower(): events for contract, events in data["events"].items()}
    return data


def _save_cache(cache: dict[str, Any], path: Path) -> None:
    """Persist the cache to disk, creating parent dirs if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Event sync
# ---------------------------------------------------------------------------


def _fetch_event_logs(
    w3: Web3,
    contracts: list[str],
    from_block: int,
    safe_head: int,
) -> tuple[dict[str, list[list[Any]]], set[int]]:
    """Fetch Lock/Free logs across a block range using adaptive chunking.

    Starts at INITIAL_CHUNK_BLOCKS; on an eth_getLogs range/size error, halves the
    window and retries down to MIN_CHUNK_BLOCKS.

    Returns:
        Tuple of (events_by_contract, new_blocks). events_by_contract maps a
        lowercased contract address to a list of [block_number, signed_wad_str],
        where Lock amounts are positive and Free amounts negative. new_blocks is the
        set of block numbers in which an event was seen.

    Raises:
        RuntimeError: if eth_getLogs fails even at MIN_CHUNK_BLOCKS.
    """
    contracts_checksummed = [Web3.to_checksum_address(c) for c in contracts]
    events_by_contract: dict[str, list[list[Any]]] = {}
    new_blocks: set[int] = set()

    block = from_block
    while block <= safe_head:
        chunk_size = INITIAL_CHUNK_BLOCKS
        while True:
            to_block = min(block + chunk_size - 1, safe_head)
            try:
                logs_lock = w3.eth.get_logs({
                    "fromBlock": block,
                    "toBlock": to_block,
                    "address": contracts_checksummed,
                    "topics": [LOCK_TOPIC],
                })
                logs_free = w3.eth.get_logs({
                    "fromBlock": block,
                    "toBlock": to_block,
                    "address": contracts_checksummed,
                    "topics": [FREE_TOPIC],
                })
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

            for log in logs_lock:
                contract_lower = log["address"].lower()
                wad = Web3.to_int(log["data"])
                events_by_contract.setdefault(contract_lower, []).append([log["blockNumber"], str(wad)])
                new_blocks.add(log["blockNumber"])
            for log in logs_free:
                contract_lower = log["address"].lower()
                wad = Web3.to_int(log["data"])
                events_by_contract.setdefault(contract_lower, []).append([log["blockNumber"], str(-wad)])
                new_blocks.add(log["blockNumber"])

            block = to_block + 1
            break

    return events_by_contract, new_blocks


def _fetch_block_timestamps(
    w3: Web3,
    blocks: set[int],
    block_timestamps: dict[str, int],
) -> dict[str, int]:
    """Fetch UNIX timestamps for any blocks not already in block_timestamps.

    Returns:
        The block_timestamps dict, extended with newly fetched entries.

    Raises:
        RuntimeError: if a get_block call fails.
    """
    for block_num in sorted(blocks):
        block_key = str(block_num)
        if block_key in block_timestamps:
            continue
        try:
            block_data = w3.eth.get_block(block_num)
        except Web3RPCError as e:
            msg = f"get_block({block_num}) failed: {e}"
            raise RuntimeError(msg) from e
        block_timestamps[block_key] = block_data["timestamp"]
    return block_timestamps


def _sync_events(
    w3: Web3,
    contracts: list[str],
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Fetch Lock/Free events from the chain, update cache, return updated cache.

    Loads the cache; if rebuild=True, discards prior events and syncs from V3_FACTORY_BLOCK.
    Otherwise syncs from (last_synced_block + 1).

    Returns:
        The updated cache dict.
    """
    cache = _load_cache(cache_path)
    if rebuild:
        cache = {"last_synced_block": None, "events": {}, "block_timestamps": {}}

    # Initialize events dict per contract (lowercase).
    for contract in contracts:
        cache.setdefault("events", {}).setdefault(contract.lower(), [])

    from_block = V3_FACTORY_BLOCK
    if not rebuild and cache.get("last_synced_block") is not None:
        from_block = max(V3_FACTORY_BLOCK, cache["last_synced_block"] + 1)

    safe_head = w3.eth.block_number - FINALITY_BLOCKS
    if from_block > safe_head:
        logger.info(
            "Already synced up to block %d; nothing new to fetch",
            cache.get("last_synced_block", V3_FACTORY_BLOCK),
        )
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
        cache["events"].setdefault(contract_lower, []).extend(events)

    cache["block_timestamps"] = _fetch_block_timestamps(w3, new_blocks, cache.get("block_timestamps", {}))
    cache["last_synced_block"] = safe_head
    _save_cache(cache, cache_path)

    logger.info(
        "Synced %d new events across %d blocks; last_synced_block=%d",
        sum(len(events) for events in cache["events"].values()),
        len(new_blocks),
        safe_head,
    )
    return cache


# ---------------------------------------------------------------------------
# Daily series building
# ---------------------------------------------------------------------------


def _contract_cumulative_balances(
    events: list[list[Any]],
    block_timestamps: dict[str, int],
    contract: str,
) -> dict[date, int]:
    """Aggregate one contract's Lock/Free events into a cumulative daily balance (wei).

    Events lacking a cached block timestamp are skipped with a warning.

    Returns:
        Mapping of event date to the running-total balance (wei) as of that date.
        Empty if the contract has no timestamped events.

    Raises:
        ValueError: if the running total goes negative, indicating missing or
            misattributed events.
    """
    events_by_date: dict[date, int] = {}
    for block_num, signed_wad_str in events:
        block_key = str(block_num)
        if block_key not in block_timestamps:
            logger.warning("Block %s has no cached timestamp; skipping event", block_num)
            continue
        event_date = datetime.fromtimestamp(block_timestamps[block_key], tz=UTC).date()
        events_by_date[event_date] = events_by_date.get(event_date, 0) + int(signed_wad_str)

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


def _build_daily_series(cache: dict[str, Any]) -> pd.DataFrame:
    """Build forward-filled daily running totals from cached events.

    For each contract, aggregates Lock/Free events into cumulative daily balances, then
    forward-fills each day between the first and last event.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with running_total_balance column.
        Forward-filled: each day carries the prior day's balance if no events occur.
    """
    rows: list[dict[str, Any]] = []
    block_timestamps = cache.get("block_timestamps", {})

    for contract, events in cache.get("events", {}).items():
        if not events:
            continue

        cumulative_by_date = _contract_cumulative_balances(events, block_timestamps, contract)
        if not cumulative_by_date:
            continue

        # Forward-fill from first event to last event.
        sorted_dates = sorted(cumulative_by_date)
        current_balance = 0
        for dt in pd.date_range(sorted_dates[0], sorted_dates[-1], freq="D").date:
            if dt in cumulative_by_date:
                current_balance = cumulative_by_date[dt]
            rows.append({
                "delegation_contract": contract,
                "dt": dt,
                "running_total_balance_wei": current_balance,
            })

    if not rows:
        # No events for any contract; return empty DataFrame with correct shape.
        return pd.DataFrame(columns=["delegation_contract", "dt", "running_total_balance_wei"]).set_index([
            "delegation_contract",
            "dt",
        ])

    df = pd.DataFrame(rows)
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    df = df.set_index(["delegation_contract", "dt"])

    # Convert wei to human-readable SKY.
    df["running_total_balance"] = df["running_total_balance_wei"].apply(lambda wei: float(Web3.from_wei(wei, "ether")))
    return df[["running_total_balance"]]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_all_sky_delegated(
    contracts: list[str],
    *,
    w3: Web3 | None = None,
    cache_path: Path | None = None,
    rebuild: bool = False,
) -> pd.DataFrame:
    """Fetch daily SKY delegations from on-chain Lock/Free events.

    Replays events from the V3 VoteDelegateFactory block, caching them to disk
    for incremental syncs. Returns one running_total_balance row per
    (delegation_contract, date), forward-filled daily.

    Args:
        contracts: list of delegate contract addresses (any case).
        w3: Web3 instance; if None, constructs from SKY_RPC_URL env var.
        cache_path: path to delegation_cache.json; defaults to output_data/.
        rebuild: if True, resyncs from V3_FACTORY_BLOCK, discarding cache.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with running_total_balance column.
        delegation_contract is lowercased; dt is datetime.date.

    Raises:
        RuntimeError: if SKY_RPC_URL is unset (and w3 not injected) or sync fails.
    """
    if w3 is None:
        rpc_url = os.environ.get("SKY_RPC_URL")
        if not rpc_url:
            raise RuntimeError(
                "SKY_RPC_URL environment variable is not set. Add it to your .env file (see .env.example).",
            )
        w3 = Web3(Web3.HTTPProvider(rpc_url))

    cache_path = cache_path or DEFAULT_CACHE_PATH

    logger.info("Syncing delegation events%s...", " (rebuild)" if rebuild else "")
    cache = _sync_events(w3, contracts, cache_path=cache_path, rebuild=rebuild)

    event_count = sum(len(events) for events in cache.get("events", {}).values())
    logger.info("Building daily running-total series from %d events", event_count)
    df = _build_daily_series(cache)
    logger.info(
        "Delegation totals cover %d days across %d contracts",
        len(df.index.get_level_values(1).unique()),
        len(df.index.get_level_values(0).unique()),
    )

    return df


def get_delegate_list_sky(
    df: pd.DataFrame,
    period: MonthPeriod,
    *,
    rebuild: bool = False,
) -> pd.DataFrame:
    """Build one row per (delegate, day) with SKY balance for the period.

    Missing daily rows from on-chain are filled with zero.

    Args:
        df: roster DataFrame with columns "Delegate Contract", "Delegate Name", "Start Date".
        period: MonthPeriod to cover.
        rebuild: if True, forces a full re-sync from V3_FACTORY_BLOCK.

    Returns:
        DataFrame with columns: contract, name, date, sky. One row per (delegate, day)
        covering every day in the period.
    """
    all_sky_delegated = get_all_sky_delegated(
        df["Delegate Contract"].tolist(),
        rebuild=rebuild,
    )

    days = list(pd.date_range(period.start, period.end, freq="D").date)
    contracts = df["Delegate Contract"].tolist()
    names_by_contract = {
        contract: name.strip().lower()
        for contract, name in zip(df["Delegate Contract"], df["Delegate Name"], strict=True)
    }

    target = pd.MultiIndex.from_product([contracts, days], names=["delegation_contract", "dt"])
    filled = all_sky_delegated["running_total_balance"].reindex(target, fill_value=0.0).astype(float).reset_index()

    return pd.DataFrame({
        "contract": filled["delegation_contract"],
        "name": filled["delegation_contract"].map(names_by_contract),
        "date": filled["dt"],
        "sky": filled["running_total_balance"],
    })


def build_sky_lookup(df_sky: pd.DataFrame) -> dict[tuple[str, date], float]:
    """Materialize df_sky into a (contract, date) -> sky-balance dict for O(1) lookup.

    Returns:
        Mapping of (contract, date) to that day's SKY balance.
    """
    lookup = df_sky.set_index(["contract", "date"])["sky"].astype(float).to_dict()
    return cast("dict[tuple[str, date], float]", lookup)


def read_sync_state(cache_path: Path | None = None) -> dict[str, Any]:
    """Read the current sync state from the cache file.

    Returns:
        Dict with keys: last_synced_block, factory_block.
    """
    cache_path = cache_path or DEFAULT_CACHE_PATH
    cache = _load_cache(cache_path)
    return {
        "last_synced_block": cache.get("last_synced_block", V3_FACTORY_BLOCK),
        "factory_block": V3_FACTORY_BLOCK,
    }
