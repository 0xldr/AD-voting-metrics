"""On-chain verification of executive votes for "Pending verification" cells.

When the public API doesn't show a delegate as a direct supporter of an
executive but they had SKY delegated at the vote's start, `sky_executive`
marks the cell "Pending verification". This module checks the chief
contract directly: did the delegate's vote-delegate contract emit a
`Vote(usr, slate)` event within 7 days of the executive going live, and
does that slate contain the executive's address? If yes, flip the cell
to "Yes". Otherwise leave it as Pending for operator adjudication.

Slate -> address-list resolution is cached persistently because slates
are immutable once etched.
"""

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pandas as pd
from eth_utils.crypto import keccak
from web3 import Web3
from web3.exceptions import BadFunctionCallOutput, ContractLogicError, Web3RPCError
from web3.types import BlockData, FilterParams, HexStr

from ad_voting_metrics.metrics import PENDING_VERIFICATION

logger = logging.getLogger(__name__)

# Sky chief / governor on mainnet.
CHIEF_ADDRESS = "0x929d9A1435662357F54AdcF64DcEE4d6b867a6f9"

# Window after spell.startDate within which a vote counts.
VOTE_DEADLINE_DAYS = 7

# Defensive cap; no real slate has approached this length.
MAX_SLATE_LENGTH = 50

# Approximate Ethereum block time. Used only to seed an `eth_getLogs`
# fromBlock — events are subsequently filtered exactly by timestamp.
SECONDS_PER_BLOCK = 12
ONE_DAY_BLOCKS = 86_400 // SECONDS_PER_BLOCK

# `Vote(address,bytes32)` event signature topic.
VOTE_EVENT_TOPIC = HexStr("0x" + keccak(text="Vote(address,bytes32)").hex())

# Sentinel returned by `slates(slate, i)` for some chiefs that don't revert
# on out-of-bounds access; treated as end-of-list.
ZERO_ADDRESS = "0x" + "0" * 40

# Minimal ABI: only the `slates` getter, used for slate -> address-list resolution.
_CHIEF_SLATES_ABI = [
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
]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_SLATE_CACHE_PATH = REPO_ROOT / "output_data" / "slate_cache.json"


# ---------------------------------------------------------------------------
# Slate cache
# ---------------------------------------------------------------------------


def _load_slate_cache(path: Path) -> dict[str, list[str]]:
    """Load the slate-hash -> [executive addresses] cache from disk.

    Returns:
        The cache; empty dict if the file doesn't exist.
    """
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return {k.lower(): [a.lower() for a in v] for k, v in data.items()}


def _save_slate_cache(cache: dict[str, list[str]], path: Path) -> None:
    """Persist the slate cache to disk, creating the parent dir if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


# ---------------------------------------------------------------------------
# Web3 calls
# ---------------------------------------------------------------------------


def _as_block(block: BlockData) -> dict[str, Any]:
    """Narrow a web3 BlockData TypedDict to a plain dict for runtime access.

    Returns:
        The same mapping, typed as dict[str, Any].
    """
    return cast("dict[str, Any]", block)


def _resolve_slate(w3: Web3, slate_hash: str) -> list[str]:
    """Walk chief.slates(slate, i) until it reverts; return the address list.

    Each exception in the catch corresponds to one end-of-list signal:
      - ContractLogicError: Solidity 0.8+ panic on out-of-bounds index
      - BadFunctionCallOutput: empty return data (older chiefs revert this way)
      - Web3RPCError: provider rejected the call
      - ValueError: web3 raised on undecodable return data

    Returns:
        Lowercased executive addresses, in slate order. Empty list if
        the slate is empty (which shouldn't happen for slates a delegate
        has actually voted for).
    """
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(CHIEF_ADDRESS),
        abi=_CHIEF_SLATES_ABI,
    )
    slate_bytes = bytes.fromhex(slate_hash.removeprefix("0x"))
    addresses: list[str] = []
    for i in range(MAX_SLATE_LENGTH):
        try:
            addr: str = contract.functions.slates(slate_bytes, i).call()
        except (BadFunctionCallOutput, ContractLogicError, Web3RPCError, ValueError):
            break
        if addr.lower() == ZERO_ADDRESS:
            break
        addresses.append(addr.lower())
    return addresses


def _approx_block_from_date(w3: Web3, target: date) -> int:
    """Estimate the block number near midnight UTC on `target`.

    Used to seed `eth_getLogs`'s fromBlock — not the actual time filter
    (the timestamp check is applied per-event after the fact). One day
    of safety buffer is subtracted so the estimate undershoots.

    Returns:
        A non-negative block number to start the log search from.
    """
    latest = _as_block(w3.eth.get_block("latest"))
    latest_ts: int = latest["timestamp"]
    latest_number: int = latest["number"]
    target_ts = int(datetime.combine(target, datetime.min.time(), tzinfo=UTC).timestamp())
    seconds_back = max(0, latest_ts - target_ts)
    blocks_back = seconds_back // SECONDS_PER_BLOCK + ONE_DAY_BLOCKS
    return max(0, latest_number - blocks_back)


def _voter_topic(voter_address: str) -> HexStr:
    """Pad a 20-byte address into a 32-byte log topic.

    Returns:
        Lowercased hex string with the 0x prefix, typed as HexStr so it
        slots into web3's FilterParams.topics without a cast.
    """
    return HexStr("0x" + voter_address.removeprefix("0x").lower().zfill(64))


def _fetch_vote_events(
    w3: Web3,
    voter_address: str,
    from_block: int,
) -> list[tuple[str, date]]:
    """Fetch (slate_hash, event_date) for one voter's chief vote events.

    Filters server-side by indexed `usr` topic, then resolves each
    event's block timestamp client-side to a UTC date.

    Returns:
        List of (slate_hash, event_date) tuples, one per Vote log.
    """
    filter_params: FilterParams = {
        "fromBlock": from_block,
        "toBlock": "latest",
        "address": Web3.to_checksum_address(CHIEF_ADDRESS),
        "topics": [VOTE_EVENT_TOPIC, _voter_topic(voter_address)],
    }
    logs = w3.eth.get_logs(filter_params)

    events: list[tuple[str, date]] = []
    block_ts_cache: dict[int, int] = {}
    for log in logs:
        slate_hex = log["topics"][2].hex()
        slate = "0x" + slate_hex.removeprefix("0x").lower()
        block_number = log["blockNumber"]
        if block_number not in block_ts_cache:
            block_ts_cache[block_number] = _as_block(w3.eth.get_block(block_number))["timestamp"]
        event_date = datetime.fromtimestamp(block_ts_cache[block_number], tz=UTC).date()
        events.append((slate, event_date))
    return events


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _identify_pending_pairs(
    df: pd.DataFrame,
    spell_addresses: list[str],
) -> dict[str, list[int]]:
    """Find the row indices in df where each spell column is "Pending verification".

    Returns:
        Mapping of spell address to the list of df row indices needing
        on-chain verification.
    """
    pending: dict[str, list[int]] = {}
    for spell_addr in spell_addresses:
        if spell_addr not in df.columns:
            continue
        mask = df[spell_addr] == PENDING_VERIFICATION
        if mask.any():
            pending[spell_addr] = df.index[mask].tolist()
    return pending


def _gather_events(
    w3: Web3,
    voters: set[str],
    from_block: int,
) -> dict[str, list[tuple[str, date]]]:
    """Fetch chief Vote events for each voter.

    Returns:
        voter address -> list of (slate, event_date) tuples.
    """
    return {voter: _fetch_vote_events(w3, voter, from_block) for voter in voters}


def _delegate_voted_for_spell(
    events: list[tuple[str, date]],
    spell_address: str,
    start_date: date,
    deadline: date,
    slate_cache: dict[str, list[str]],
) -> bool:
    """Return True if any in-window event's slate contains the spell address."""
    spell_address = spell_address.lower()
    for slate, event_date in events:
        if not (start_date <= event_date <= deadline):
            continue
        if spell_address in slate_cache.get(slate, []):
            return True
    return False


def _flip_eligible_cells(
    df: pd.DataFrame,
    spell_info: list[dict],
    pending: dict[str, list[int]],
    events_by_voter: dict[str, list[tuple[str, date]]],
    slate_cache: dict[str, list[str]],
) -> int:
    """Mutate df: set Pending cells to "Yes" where a slate confirms the vote.

    Returns:
        The count of cells flipped.
    """
    flipped = 0
    for spell in spell_info:
        spell_addr = spell["address"]
        start = spell["startDate"]
        deadline = start + timedelta(days=VOTE_DEADLINE_DAYS)
        for idx in pending.get(spell_addr, []):
            voter = str(df.at[idx, "Delegate Contract"])  # noqa: PD008
            if _delegate_voted_for_spell(events_by_voter.get(voter, []), spell_addr, start, deadline, slate_cache):
                df.at[idx, spell_addr] = "Yes"  # noqa: PD008
                flipped += 1
    return flipped


def resolve_pending_executive_votes(
    df: pd.DataFrame,
    spell_info: list[dict],
    *,
    w3: Web3 | None = None,
    cache_path: Path | None = None,
) -> pd.DataFrame:
    """Flip "Pending verification" cells to "Yes" for verifiable on-chain votes.

    For each (delegate, spell) cell currently "Pending verification":
      - Fetch the delegate's chief Vote events from on-chain.
      - For each event with `spell.startDate <= event.date <=
        spell.startDate + 7 days`, look up the slate's executive list
        (cached). If the spell's address is in the slate, flip the cell.

    No-ops gracefully when SKY_RPC_URL is missing, when spell_info is
    empty, or when no cells are pending. Other errors (RPC failures,
    decode errors) propagate.

    Returns:
        The same df (mutated) with verifiable Pending cells set to "Yes".
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
                "SKY_RPC_URL is not set; leaving %d Pending Verification cell(s) as-is. "
                "Set SKY_RPC_URL in .env to enable on-chain verification.",
                sum(len(v) for v in pending.values()),
            )
            return df
        w3 = Web3(Web3.HTTPProvider(rpc_url))

    cache_path = cache_path or DEFAULT_SLATE_CACHE_PATH
    slate_cache = _load_slate_cache(cache_path)
    initial_cache_size = len(slate_cache)

    earliest_start = min(s["startDate"] for s in spell_info)
    from_block = _approx_block_from_date(w3, earliest_start)
    logger.info("Verifying executive votes on-chain from block %d onwards", from_block)

    voters: set[str] = {
        str(df.at[idx, "Delegate Contract"])  # noqa: PD008 — .at is correct for scalar access
        for indices in pending.values()
        for idx in indices
    }
    events_by_voter = _gather_events(w3, voters, from_block)

    seen_slates = {slate for events in events_by_voter.values() for slate, _ in events}
    for slate in seen_slates - slate_cache.keys():
        slate_cache[slate] = _resolve_slate(w3, slate)

    flipped = _flip_eligible_cells(df, spell_info, pending, events_by_voter, slate_cache)

    if len(slate_cache) > initial_cache_size:
        _save_slate_cache(slate_cache, cache_path)
        logger.info("Slate cache grew by %d entries (now %d)", len(slate_cache) - initial_cache_size, len(slate_cache))

    logger.info(
        "On-chain verification flipped %d of %d Pending Verification cell(s) to 'Yes'",
        flipped, sum(len(v) for v in pending.values()),
    )
    return df
