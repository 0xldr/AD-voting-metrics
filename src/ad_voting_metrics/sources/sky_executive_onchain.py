"""On-chain adjudication of executive votes for "Pending verification" cells.

`sky_executive` marks a cell "Pending verification" whenever a delegate had SKY delegated when the executive went live,
leaving open whether they voted and when. This module answers both from the chief contract: did the delegate's
vote-delegate contract emit a `Vote(usr, slate)` event whose slate contains the executive's address, and on what day?

  - Earliest such vote on or before the deadline -> "Yes"
  - Earliest such vote after the deadline        -> "Late" (counted as non-participation)
  - No such vote found                           -> left Pending for operator adjudication

The deadline is 3 business days after the spell goes live (`vote_status.spell_vote_deadline`). Timing can only be
established on-chain, so with no RPC configured every cell stays Pending rather than being credited unverified.

Slate -> address-list resolution is cached persistently because slates are immutable once etched.
"""

import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import pandas as pd
from eth_typing import HexStr
from web3 import Web3
from web3.constants import ADDRESS_ZERO
from web3.exceptions import BadFunctionCallOutput, ContractLogicError, Web3RPCError

from ad_voting_metrics.paths import SLATE_CACHE_PATH
from ad_voting_metrics.sources.json_cache import load_json_cache, save_json_cache
from ad_voting_metrics.vote_status import LATE, PENDING_VERIFICATION, YES, spell_vote_deadline

logger = logging.getLogger(__name__)

# Sky chief / governor on mainnet.
CHIEF_ADDRESS = "0x929d9A1435662357F54AdcF64DcEE4d6b867a6f9"

# Defensive cap; no real slate has approached this length.
MAX_SLATE_LENGTH = 50

# Minimal chief ABI: the `slates` getter for slate -> address-list
# resolution, and the `Vote` event so contract.events.Vote can encode
# argument filters and decode log topics for us.
_CHIEF_ABI = [
    {
        "name": "slates",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "", "type": "bytes32"},
            {"name": "", "type": "uint256"},
        ],
        "outputs": [{"name": "yays", "type": "address"}],
    },
    {
        "name": "Vote",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "usr", "type": "address", "indexed": True},
            {"name": "slate", "type": "bytes32", "indexed": True},
        ],
    },
]

# ---------------------------------------------------------------------------
# Slate cache
# ---------------------------------------------------------------------------


def _load_slate_cache(path: Path) -> dict[str, list[str]]:
    """Load the slate-hash -> [executive addresses] cache from disk.

    Returns the cache, with hashes and addresses lowercased; empty dict if the file doesn't exist.
    """
    data = load_json_cache(path)
    return {k.lower(): [a.lower() for a in v] for k, v in data.items()}


def _save_slate_cache(cache: dict[str, list[str]], path: Path) -> None:
    """Persist the slate cache to disk atomically, creating the parent dir if needed."""
    save_json_cache(cast("dict[str, Any]", cache), path)


# ---------------------------------------------------------------------------
# Web3 calls
# ---------------------------------------------------------------------------


def _resolve_slate(w3: Web3, slate_hash: str) -> list[str]:
    """Walk chief.slates(slate, i) until it reverts; return the address list.

    Each exception in the catch corresponds to one end-of-list signal:
      - ContractLogicError: Solidity 0.8+ panic on out-of-bounds index
      - BadFunctionCallOutput: empty return data (older chiefs revert this way)
      - Web3RPCError: provider rejected the call
      - ValueError: web3 raised on undecodable return data

    Returns lowercased executive addresses, in slate order. Empty list if the slate is empty (which shouldn't happen
    for slates a delegate has actually voted for).
    """
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CHIEF_ADDRESS),
        abi=_CHIEF_ABI,
    )
    slate_bytes = Web3.to_bytes(hexstr=HexStr(slate_hash))
    addresses: list[str] = []
    for i in range(MAX_SLATE_LENGTH):
        try:
            addr: str = contract.functions.slates(slate_bytes, i).call()
        except BadFunctionCallOutput, ContractLogicError, Web3RPCError, ValueError:
            break
        if addr.lower() == ADDRESS_ZERO:
            break
        addresses.append(addr.lower())
    return addresses


def _block_from_date(w3: Web3, target: date) -> int:
    """Return the first block number at or after midnight UTC on `target`.

    Binary search over block timestamps via the RPC (~25 get_block calls for mainnet). Seeds `eth_getLogs`'s
    fromBlock — events are still filtered per-event by exact date afterwards, so the only requirement is that the
    block is no later than the earliest event we care about.
    """
    target_ts = int(datetime.combine(target, datetime.min.time(), tzinfo=UTC).timestamp())
    lo, hi = 1, int(w3.eth.block_number)
    while lo < hi:
        mid = (lo + hi) // 2
        # `timestamp` is NotRequired on BlockData per web3-stubs, but full
        # blocks always carry it at runtime; widen to plain dict for access.
        mid_ts = cast("dict[str, Any]", w3.eth.get_block(mid))["timestamp"]
        if mid_ts < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _fetch_vote_events(
    w3: Web3,
    voters: set[str],
    from_block: int,
) -> dict[str, list[tuple[str, date]]]:
    """Fetch chief Vote events for every voter in one eth_getLogs call.

    Passes the voter set as an OR-filter on the indexed `usr` argument so a single RPC roundtrip returns events for all
    voters together, then groups them client-side by lowercased voter address. Voters with no events in the window get
    an empty list. web3's contract.events handles topic encoding and decoding.

    Returns lowercased voter address -> list of (slate_hash, event_date) tuples.
    """
    result: dict[str, list[tuple[str, date]]] = {v.lower(): [] for v in voters}
    if not voters:
        return result

    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CHIEF_ADDRESS),
        abi=_CHIEF_ABI,
    )
    entries = contract.events.Vote().get_logs(
        from_block=from_block,
        argument_filters={"usr": [Web3.to_checksum_address(v) for v in voters]},
    )

    block_ts_cache: dict[int, int] = {}
    for entry in entries:
        voter = entry["args"]["usr"].lower()
        slate = Web3.to_hex(entry["args"]["slate"])
        block_number = entry["blockNumber"]
        if block_number not in block_ts_cache:
            # `timestamp` is NotRequired on BlockData per web3-stubs, but full
            # blocks always carry it at runtime; widen to plain dict for access.
            block = cast("dict[str, Any]", w3.eth.get_block(block_number))
            block_ts_cache[block_number] = block["timestamp"]
        event_date = datetime.fromtimestamp(block_ts_cache[block_number], tz=UTC).date()
        result.setdefault(voter, []).append((slate, event_date))
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _identify_pending_pairs(
    df: pd.DataFrame,
    spell_addresses: list[str],
) -> dict[str, list[int]]:
    """Find the row indices in df where each spell column is "Pending verification".

    Returns a mapping of spell address to the list of df row indices needing on-chain verification.
    """
    pending: dict[str, list[int]] = {}
    for spell_addr in spell_addresses:
        if spell_addr not in df.columns:
            continue
        mask = df[spell_addr] == PENDING_VERIFICATION
        if mask.any():
            pending[spell_addr] = df.index[mask].tolist()
    return pending


def _first_vote_date_for_spell(
    events: list[tuple[str, date]],
    spell_address: str,
    start_date: date,
    slate_cache: dict[str, list[str]],
) -> date | None:
    """Return the earliest date on or after start_date on which an event's slate contained the spell address.

    The earliest qualifying vote is the one that decides on-time versus late: a delegate who votes in time and later
    re-slates has still met the deadline.

    Returns:
        The vote date, or None if no event's slate contains the spell.
    """
    spell_address = spell_address.lower()
    dates = [
        event_date
        for slate, event_date in events
        if event_date >= start_date and spell_address in slate_cache.get(slate, [])
    ]
    return min(dates, default=None)


def _adjudicate_cells(
    df: pd.DataFrame,
    spell_info: list[dict],
    pending: dict[str, list[int]],
    events_by_voter: dict[str, list[tuple[str, date]]],
    slate_cache: dict[str, list[str]],
) -> tuple[int, int]:
    """Mutate df: resolve Pending cells to "Yes" or "Late" where on-chain events settle the question.

    Cells with no matching vote event are left Pending.

    Returns:
        (on_time_count, late_count).
    """
    on_time = late = 0
    for spell in spell_info:
        spell_addr = spell["address"]
        start = spell["startDate"]
        deadline = spell_vote_deadline(start)
        for idx in pending.get(spell_addr, []):
            voter = str(df.at[idx, "Delegate Contract"])
            vote_date = _first_vote_date_for_spell(events_by_voter.get(voter, []), spell_addr, start, slate_cache)
            if vote_date is None:
                continue
            if vote_date <= deadline:
                df.at[idx, spell_addr] = YES
                on_time += 1
            else:
                df.at[idx, spell_addr] = LATE
                late += 1
    return on_time, late


def resolve_pending_executive_votes(
    df: pd.DataFrame,
    spell_info: list[dict],
    *,
    w3: Web3 | None = None,
    cache_path: Path | None = None,
) -> pd.DataFrame:
    """Resolve "Pending verification" cells to "Yes" or "Late" from on-chain evidence.

    For each (delegate, spell) cell currently "Pending verification":
      - Fetch the delegate's chief Vote events from on-chain.
      - Take the earliest event at or after `spell.startDate` whose slate (cached) contains the spell address.
      - On or before `spell_vote_deadline(startDate)` -> "Yes"; after it -> "Late"; no such event -> left Pending.

    No-ops gracefully when SKY_RPC_URL is missing, when spell_info is empty, or when no cells are pending. Leaving
    cells Pending is the deliberate no-RPC outcome: a vote's timing cannot be established off-chain, so nothing is
    credited without evidence. Other errors (RPC failures, decode errors) propagate.

    Returns:
        The same df (mutated) with resolvable Pending cells set to "Yes" or "Late".
    """
    if not spell_info:
        return df

    pending = _identify_pending_pairs(df, [s["address"] for s in spell_info])
    if not pending:
        logger.info("No 'Pending verification' executive cells to verify on-chain.")
        return df

    if w3 is None:
        rpc_url = os.environ.get("SKY_RPC_URL")
        if not rpc_url:
            logger.warning(
                "SKY_RPC_URL is not set; leaving all %d spell cell(s) as Pending Verification. Vote timing is only "
                "establishable on-chain, so no spell vote can be credited without it. Set SKY_RPC_URL in .env.",
                sum(len(v) for v in pending.values()),
            )
            return df
        w3 = Web3(Web3.HTTPProvider(rpc_url))

    cache_path = cache_path or SLATE_CACHE_PATH
    slate_cache = _load_slate_cache(cache_path)
    initial_cache_size = len(slate_cache)

    earliest_start = min(s["startDate"] for s in spell_info)
    from_block = _block_from_date(w3, earliest_start)
    logger.info("Verifying executive votes on-chain from block %d onwards", from_block)

    voters: set[str] = {str(df.at[idx, "Delegate Contract"]) for indices in pending.values() for idx in indices}
    events_by_voter = _fetch_vote_events(w3, voters, from_block)

    seen_slates = {slate for events in events_by_voter.values() for slate, _ in events}
    for slate in seen_slates - slate_cache.keys():
        slate_cache[slate] = _resolve_slate(w3, slate)

    on_time, late = _adjudicate_cells(df, spell_info, pending, events_by_voter, slate_cache)

    if len(slate_cache) > initial_cache_size:
        _save_slate_cache(slate_cache, cache_path)
        logger.info("Slate cache grew by %d entries (now %d)", len(slate_cache) - initial_cache_size, len(slate_cache))

    total_pending = sum(len(v) for v in pending.values())
    logger.info(
        "On-chain adjudication of %d Pending Verification cell(s): %d on time, %d late, %d still pending",
        total_pending,
        on_time,
        late,
        total_pending - on_time - late,
    )
    return df
