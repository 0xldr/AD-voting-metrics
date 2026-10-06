"""Executive spells: the period's listing from vote.sky.money, balance-seeded statuses, and on-chain settlement.

A delegate who had SKY delegated when a spell went live is seeded "Pending verification", leaving open whether they
voted and when. The chief contract answers both: did the delegate's vote-delegate contract emit a `Vote(usr, slate)`
event whose slate contains the spell's address, and on what day?

  - Earliest such vote on or before the deadline -> "Yes"
  - Earliest such vote after the deadline        -> "Late" (counted as non-participation)
  - No such vote found                           -> left Pending for operator adjudication

The deadline is 3 business days after the spell goes live (`ballots.spell_vote_deadline`). Timing can only be
established on-chain, so a vote the check cannot find is never credited; the cell stays Pending.

Slate -> address-list resolution is cached persistently because slates are immutable once etched.
"""

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from eth_typing import HexStr
from web3 import Web3
from web3.constants import ADDRESS_ZERO
from web3.contract import Contract
from web3.exceptions import BadFunctionCallOutput, ContractLogicError, Web3RPCError

from ad_voting_metrics.ballots import (
    EXITED,
    LATE,
    NO_DELEGATED_SKY,
    NOT_STARTED,
    PENDING_VERIFICATION,
    YES,
    Ballot,
    Statuses,
    exited_before,
    spell_vote_deadline,
    voted_while_aligned,
)
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.sources.chain import block_timestamp, fetch_block_timestamps, safe_head, utc_date
from ad_voting_metrics.sources.json_cache import load_json_cache, save_json_cache

from .http import fetch_json, paginate

logger = logging.getLogger(__name__)

SKY_EXECUTIVE_URL = "https://vote.sky.money/api/executive"

# The API's default page size for the executive listing, which paginates by absolute `start` offset.
SKY_EXECUTIVES_PAGE_SIZE = 100

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


def fetch_spells_for_period(period: MonthPeriod) -> list[Ballot]:
    """Fetch executive spells from vote.sky.money that went live within the period, as Ballots with no end date.

    The listing is newest-first, so paging stops at the first spell dated before the period.
    """

    def executives_page(offset: int) -> list[dict[str, Any]]:
        return list(fetch_json(SKY_EXECUTIVE_URL, start=offset, limit=SKY_EXECUTIVES_PAGE_SIZE))

    spells: list[Ballot] = []
    for page in paginate(executives_page, first=0, step=SKY_EXECUTIVES_PAGE_SIZE):
        for execute in page:
            live = datetime.fromisoformat(execute["date"]).date()
            if live < period.start:
                logger.info("Fetched %d executive spells going live in %s", len(spells), period)
                return spells
            if live <= period.end:
                spells.append(
                    Ballot(
                        id=execute["address"].lower(),
                        start=live,
                        end=None,
                        title=execute["title"],
                    )
                )

    logger.info("Fetched %d executive spells going live in %s", len(spells), period)
    return spells


def spell_statuses(
    spells: list[Ballot],
    delegates: list[Delegate],
    sky_lookup: dict[tuple[str, date], float],
) -> Statuses:
    """Seed each (delegate, spell) status from SKY balance and alignment dates.

      - Aligned after the spell went live         -> "Not Started"
      - Exited before the spell went live         -> "Exited"
      - No SKY delegated on the spell's start day -> "No Delegated SKY"
      - Otherwise                                 -> "Pending verification"

    Whether a delegate actually voted, and whether they did so inside the deadline, is settled by
    `resolve_pending_executive_votes` against chief Vote events. The public supporters endpoint reports only who
    currently supports a spell, with no timestamp, so it cannot answer the deadline question and is not consulted.
    """
    statuses: Statuses = {}
    for spell in spells:
        for delegate in delegates:
            contract = delegate.vote_delegate_address
            if delegate.start_date > spell.start:
                status = NOT_STARTED
            elif exited_before(delegate.end_date, spell.start):
                status = EXITED
            elif sky_lookup.get((contract, spell.start), 0.0) == 0:
                status = NO_DELEGATED_SKY
            else:
                status = PENDING_VERIFICATION
            statuses[contract, spell.id] = status
    return statuses


def _chief(w3: Web3) -> Contract:
    """Return the chief contract bound to the minimal ABI."""
    return w3.eth.contract(address=Web3.to_checksum_address(CHIEF_ADDRESS), abi=_CHIEF_ABI)


def _resolve_slate(w3: Web3, slate_hash: str) -> list[str]:
    """Walk chief.slates(slate, i) until it reverts; return the address list.

    A ContractLogicError is the normal end of the list (Solidity 0.8+ panics on an out-of-bounds index). Three other
    signals also end the walk and are logged at DEBUG, since they may indicate a provider or decode fault rather than
    the true end: BadFunctionCallOutput (empty return data, how older chiefs revert), Web3RPCError (provider rejected
    the call), and ValueError (undecodable return data). A truncated slate can only leave a cell Pending, never credit
    a vote.

    Returns lowercased executive addresses, in slate order. Empty list if the slate is empty (which shouldn't happen
    for slates a delegate has actually voted for).
    """
    slates = _chief(w3).functions.slates
    slate_bytes = Web3.to_bytes(hexstr=HexStr(slate_hash))
    addresses: list[str] = []
    for i in range(MAX_SLATE_LENGTH):
        try:
            addr: str = slates(slate_bytes, i).call()
        except ContractLogicError:
            break
        except (BadFunctionCallOutput, Web3RPCError, ValueError) as e:
            logger.debug("Slate %s walk stopped at index %d on %s: %s", slate_hash, i, type(e).__name__, e)
            break
        if addr.lower() == ADDRESS_ZERO:
            break
        addresses.append(addr.lower())
    return addresses


def _block_from_date(w3: Web3, target: date, known_timestamps: dict[int, int], *, to_block: int) -> int:
    """Return a block number no later than the first block on `target` (midnight UTC), capped at `to_block`.

    Seeds `eth_getLogs`'s fromBlock; events are filtered per-event by exact date afterwards, so any block at or before
    the true first block works. When `known_timestamps` (block -> UNIX timestamp, typically the delegation cache) has a
    block before the target, the latest such block is returned with no RPC calls. Otherwise a binary search over block
    timestamps via the RPC finds the first block at or after midnight (~25 get_block calls for mainnet).
    """
    target_ts = int(datetime.combine(target, datetime.min.time(), tzinfo=UTC).timestamp())
    known_before = [block for block, ts in known_timestamps.items() if ts < target_ts]
    if known_before:
        return max(known_before)

    lo, hi = 1, to_block
    while lo < hi:
        mid = (lo + hi) // 2
        if block_timestamp(w3.eth.get_block(mid)) < target_ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _fetch_vote_events(
    w3: Web3,
    voters: set[str],
    from_block: int,
    to_block: int,
    block_timestamps: dict[int, int],
) -> dict[str, list[tuple[str, date]]]:
    """Fetch chief Vote events for every voter between from_block and to_block in one eth_getLogs call.

    Passes the voter set as an OR-filter on the indexed `usr` argument so a single RPC roundtrip returns events for all
    voters together, then groups them client-side by lowercased voter address. Voters with no events in the window get
    an empty list. web3's contract.events handles topic encoding and decoding. Events are dated through
    `block_timestamps`, which is extended in place with any blocks it lacks.

    Returns lowercased voter address -> list of (slate_hash, event_date) tuples.
    """
    result: dict[str, list[tuple[str, date]]] = {v: [] for v in voters}
    if not voters:
        return result

    vote_event = _chief(w3).events.Vote()
    entries = vote_event.get_logs(
        from_block=from_block,
        to_block=to_block,
        argument_filters={"usr": [Web3.to_checksum_address(v) for v in voters]},
    )
    fetch_block_timestamps(w3, {entry["blockNumber"] for entry in entries}, block_timestamps)

    for entry in entries:
        voter = entry["args"]["usr"].lower()
        slate = Web3.to_hex(entry["args"]["slate"])
        event_date = utc_date(block_timestamps[entry["blockNumber"]])
        result.setdefault(voter, []).append((slate, event_date))
    return result


def _pending_pairs(statuses: Statuses, spells: list[Ballot]) -> set[tuple[str, str]]:
    """Return the (delegate contract, spell address) pairs whose status is "Pending verification"."""
    spell_ids = {spell.id for spell in spells}
    return {key for key, status in statuses.items() if status == PENDING_VERIFICATION and key[1] in spell_ids}


def _first_vote_date_for_spell(
    events: list[tuple[str, date]],
    spell_address: str,
    start_date: date,
    slate_cache: dict[str, list[str]],
) -> date | None:
    """Return the earliest date on or after start_date on which an event's slate contained the spell address.

    The earliest qualifying vote is the one that decides on-time versus late: a delegate who votes in time and later
    re-slates has still met the deadline. None if no event's slate contains the spell.
    """
    dates = [
        event_date
        for slate, event_date in events
        if event_date >= start_date and spell_address in slate_cache.get(slate, [])
    ]
    return min(dates, default=None)


def resolve_pending_executive_votes(  # noqa: PLR0913 — three data inputs plus the client and its two cache seeds
    statuses: Statuses,
    spells: list[Ballot],
    delegates: list[Delegate],
    *,
    w3: Web3,
    cache_path: Path,
    known_block_timestamps: dict[int, int],
) -> Statuses:
    """Resolve "Pending verification" cells to "Yes", "Late", or "Exited" from on-chain evidence.

    For each (delegate, spell) pair currently "Pending verification":
      - Fetch the delegate's chief Vote events from on-chain.
      - Take the earliest event at or after the spell's start whose slate (cached) contains the spell address, ignoring
        any cast after the delegate's inclusive last aligned day.
      - On or before `spell_vote_deadline(start)` -> "Yes"; after it -> "Late".
      - No such event: "Exited" if the delegate left before the deadline, otherwise left Pending.

    `known_block_timestamps` (block -> UNIX timestamp) lets the event fetch start from an already-dated block instead
    of binary-searching the chain, and dates events whose blocks it already holds without an RPC call; pass an empty
    dict to force the search. It is not mutated. Makes no RPC calls when nothing is pending. Reads stop FINALITY_BLOCKS
    behind the head, matching the delegation sync, so a shallow reorg cannot flip a cell between runs. RPC and decode
    errors propagate. The input mapping is not mutated; a new one comes back with resolvable Pending
    cells set to "Yes" or "Late" and every other entry unchanged.
    """
    pending = _pending_pairs(statuses, spells)
    if not pending:
        logger.info("No 'Pending verification' executive cells to verify on-chain.")
        return statuses

    slate_cache: dict[str, list[str]] = load_json_cache(cache_path)
    initial_cache_size = len(slate_cache)
    block_timestamps = dict(known_block_timestamps)
    head = safe_head(w3)

    earliest_start = min(spell.start for spell in spells)
    from_block = _block_from_date(w3, earliest_start, block_timestamps, to_block=head)
    logger.info("Verifying executive votes on-chain from block %d to %d", from_block, head)

    voters = {contract for contract, _ in pending}
    events_by_voter = _fetch_vote_events(w3, voters, from_block, head, block_timestamps)

    seen_slates = {slate for events in events_by_voter.values() for slate, _ in events}
    for slate in seen_slates - slate_cache.keys():
        slate_cache[slate] = _resolve_slate(w3, slate)

    spells_by_id = {spell.id: spell for spell in spells}
    end_dates = {d.vote_delegate_address: d.end_date for d in delegates}
    resolved: Statuses = {}
    for contract, spell_id in pending:
        spell = spells_by_id[spell_id]
        deadline = spell_vote_deadline(spell.start)
        vote_date = _first_vote_date_for_spell(events_by_voter.get(contract, []), spell.id, spell.start, slate_cache)
        if voted_while_aligned(vote_date, end_dates.get(contract)):
            resolved[contract, spell_id] = YES if vote_date is not None and vote_date <= deadline else LATE
        elif exited_before(end_dates.get(contract), deadline):
            resolved[contract, spell_id] = EXITED

    if len(slate_cache) > initial_cache_size:
        save_json_cache(slate_cache, cache_path)
        logger.info("Slate cache grew by %d entries (now %d)", len(slate_cache) - initial_cache_size, len(slate_cache))

    tally = {status: sum(1 for s in resolved.values() if s == status) for status in (YES, LATE, EXITED)}
    logger.info(
        "On-chain adjudication of %d Pending Verification cell(s): %d on time, %d late, %d exited, %d still pending",
        len(pending),
        tally[YES],
        tally[LATE],
        tally[EXITED],
        len(pending) - len(resolved),
    )
    return statuses | resolved
