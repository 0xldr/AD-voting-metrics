"""Tests for the on-chain executive-vote verifier.

All w3 calls are mocked — no real RPC traffic. The slate cache uses a
tmp_path file in each test that needs it.
"""

import json
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.sources import sky_executive_onchain as onchain

PENDING = "Pending verification"

_VOTER_1 = "0x" + "01" * 20
_VOTER_2 = "0x" + "02" * 20


def _ts(d: date) -> int:
    """Unix timestamp for midnight UTC on `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _spell(addr: str, start: date) -> Ballot:
    return Ballot(id=addr, kind="spell", start=start, end=None, title="x")


# ---------------------------------------------------------------------------
# Slate cache I/O
# ---------------------------------------------------------------------------


def test_load_slate_cache_missing_file_returns_empty(tmp_path):
    assert onchain._load_slate_cache(tmp_path / "nope.json") == {}


def test_load_slate_cache_lowercases_keys_and_addresses(tmp_path):
    cache_path = tmp_path / "slate_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "0xABCDEF": ["0x1111", "0x2222"],
                "0xBEEF": ["0x3333"],
            }
        )
    )
    out = onchain._load_slate_cache(cache_path)
    assert out == {
        "0xabcdef": ["0x1111", "0x2222"],
        "0xbeef": ["0x3333"],
    }


def test_save_slate_cache_round_trip(tmp_path):
    cache_path = tmp_path / "nested" / "slate_cache.json"  # parent doesn't exist
    cache = {"0xabc": ["0x111", "0x222"], "0xdef": ["0x333"]}
    onchain._save_slate_cache(cache, cache_path)
    assert cache_path.exists()
    loaded = onchain._load_slate_cache(cache_path)
    assert loaded == cache


# ---------------------------------------------------------------------------
# _resolve_slate
# ---------------------------------------------------------------------------


def _make_w3_with_slates(slate_addresses: list[str]) -> MagicMock:
    """Build a fake w3 whose chief.slates(slate, i) returns slate_addresses[i],
    raising ContractLogicError when i >= len(slate_addresses).
    """
    w3 = MagicMock()
    contract = MagicMock()

    def _slates_call(_slate, i):
        call = MagicMock()
        if i < len(slate_addresses):
            call.call.return_value = slate_addresses[i]
        else:
            call.call.side_effect = ContractLogicError("array out of bounds")
        return call

    contract.functions.slates.side_effect = _slates_call
    w3.eth.contract.return_value = contract
    return w3


def test_resolve_slate_walks_until_revert_and_lowercases():
    w3 = _make_w3_with_slates(
        [
            "0x1111111111111111111111111111111111111111",
            "0xAABBccDDeeFF00112233445566778899AABBCCDD",  # mixed case from the RPC
            "0x3333333333333333333333333333333333333333",
        ]
    )
    out = onchain._resolve_slate(w3, "0xabcd")
    assert out == [
        "0x1111111111111111111111111111111111111111",
        "0xaabbccddeeff00112233445566778899aabbccdd",
        "0x3333333333333333333333333333333333333333",
    ]


def test_resolve_slate_stops_at_zero_address():
    """Some chiefs sentinel-terminate with the zero address rather than reverting."""
    w3 = _make_w3_with_slates(
        [
            "0x1111111111111111111111111111111111111111",
            "0x0000000000000000000000000000000000000000",  # sentinel
            "0xshouldNotReachHere",
        ]
    )
    out = onchain._resolve_slate(w3, "0xabcd")
    assert out == ["0x1111111111111111111111111111111111111111"]


def test_resolve_slate_safety_cap_on_runaway():
    """MAX_SLATE_LENGTH bounds the walk if the contract never reverts."""
    w3 = _make_w3_with_slates(["0x1111111111111111111111111111111111111111"] * 100)
    out = onchain._resolve_slate(w3, "0xabcd")
    assert len(out) == onchain.MAX_SLATE_LENGTH


# ---------------------------------------------------------------------------
# _block_from_date
# ---------------------------------------------------------------------------


def test_block_from_date_finds_first_block_at_or_after_midnight():
    """Binary search converges on the first block whose timestamp >= midnight UTC of the target date."""
    genesis_ts = _ts(date(2026, 5, 1))
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 200_000
    # One block every 12 seconds from genesis_ts.
    mock_w3.eth.get_block.side_effect = lambda n: {"timestamp": genesis_ts + n * 12}

    result = onchain._block_from_date(mock_w3, date(2026, 5, 13), {})

    # 12 days * 86400 s / 12 s per block — timestamp lands exactly on midnight, so that block is included.
    assert result == 12 * 86400 // 12
    # Binary search, not a linear scan: far fewer calls than the block range.
    assert mock_w3.eth.get_block.call_count < 25


def test_block_from_date_returns_first_block_when_chain_starts_after_target():
    """A chain whose first block already postdates the target collapses to block 1."""
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 1_000
    mock_w3.eth.get_block.side_effect = lambda n: {"timestamp": _ts(date(2026, 6, 1)) + n}

    assert onchain._block_from_date(mock_w3, date(2026, 5, 13), {}) == 1


def test_block_from_date_uses_latest_known_block_before_target_without_rpc():
    """A cached block dated before the target is returned as-is; blocks at or after the target are ignored."""
    mock_w3 = MagicMock()
    target = date(2026, 5, 13)
    known = {100: _ts(date(2026, 5, 10)), 250: _ts(target) - 60, 300: _ts(target) + 60}

    assert onchain._block_from_date(mock_w3, target, known) == 250
    mock_w3.eth.get_block.assert_not_called()


# ---------------------------------------------------------------------------
# _fetch_vote_events
# ---------------------------------------------------------------------------


def _make_event(slate_hex: str, block_number: int, voter: str = _VOTER_1) -> dict:
    """Build a decoded EventData dict matching what contract.events.Vote.get_logs returns."""
    return {
        "args": {
            "usr": Web3.to_checksum_address(voter),
            "slate": HexBytes(bytes.fromhex(slate_hex.removeprefix("0x").zfill(64))),
        },
        "blockNumber": block_number,
    }


def _patch_contract_events(w3: MagicMock, events: list[dict]) -> None:
    """Configure w3.eth.contract(...).events.Vote().get_logs to return `events`."""
    contract = w3.eth.contract.return_value
    vote_event = contract.events.Vote.return_value
    vote_event.get_logs.return_value = events


def test_fetch_vote_events_makes_one_get_logs_call_for_n_voters():
    """The bulk fetch passes voters as an OR'd argument filter, not one call per voter."""
    w3 = MagicMock()
    _patch_contract_events(w3, events=[])
    onchain._fetch_vote_events(w3, {_VOTER_1, _VOTER_2}, from_block=500)
    vote_event = w3.eth.contract.return_value.events.Vote.return_value
    assert vote_event.get_logs.call_count == 1
    # `usr` filter is a list of two checksum addresses.
    kwargs = vote_event.get_logs.call_args.kwargs
    usr_filter = kwargs["argument_filters"]["usr"]
    assert isinstance(usr_filter, list)
    assert len(usr_filter) == 2


def test_fetch_vote_events_groups_results_by_voter():
    w3 = MagicMock()
    _patch_contract_events(
        w3,
        events=[
            _make_event("0xabcd", 1000, voter=_VOTER_1),
            _make_event("0xbeef", 1001, voter=_VOTER_2),
        ],
    )
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 5, 20))}
    out = onchain._fetch_vote_events(w3, {_VOTER_1, _VOTER_2}, from_block=500)
    assert _VOTER_1 in out
    assert _VOTER_2 in out
    assert out[_VOTER_1][0][0].endswith("abcd")
    assert out[_VOTER_2][0][0].endswith("beef")


def test_fetch_vote_events_voters_with_no_events_get_empty_list():
    w3 = MagicMock()
    _patch_contract_events(
        w3,
        events=[
            _make_event("0xabcd", 1000, voter=_VOTER_1),
        ],
    )
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 5, 20))}
    out = onchain._fetch_vote_events(w3, {_VOTER_1, _VOTER_2}, from_block=500)
    assert out[_VOTER_2] == []


def test_fetch_vote_events_empty_voter_set_skips_rpc():
    w3 = MagicMock()
    out = onchain._fetch_vote_events(w3, set(), from_block=500)
    assert out == {}
    w3.eth.contract.assert_not_called()


def test_fetch_vote_events_caches_block_timestamps():
    """Same block number across multiple logs => one get_block call."""
    w3 = MagicMock()
    _patch_contract_events(
        w3,
        events=[
            _make_event("0xabcd", 1000, voter=_VOTER_1),
            _make_event("0xbeef", 1000, voter=_VOTER_1),
            _make_event("0xcafe", 1000, voter=_VOTER_2),
        ],
    )
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 5, 20))}
    onchain._fetch_vote_events(w3, {_VOTER_1, _VOTER_2}, from_block=500)
    assert w3.eth.get_block.call_count == 1


# ---------------------------------------------------------------------------
# _pending_pairs
# ---------------------------------------------------------------------------


def test_pending_pairs_only_flags_pending_cells_for_known_spells():
    statuses = {
        ("0xa", "0xspell1"): "Yes",
        ("0xb", "0xspell1"): PENDING,
        ("0xc", "0xspell1"): "No",
        ("0xa", "0xspell2"): PENDING,
        ("0xb", "0xspell2"): PENDING,
        ("0xa", "0xspell_unknown"): PENDING,  # not in the spell list: ignored
    }
    spells = [_spell("0xspell1", date(2026, 4, 1)), _spell("0xspell2", date(2026, 4, 8))]

    assert onchain._pending_pairs(statuses, spells) == {("0xb", "0xspell1"), ("0xa", "0xspell2"), ("0xb", "0xspell2")}


# ---------------------------------------------------------------------------
# _first_vote_date_for_spell
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("events", "spell_address", "slate_cache", "expected"),
    [
        pytest.param([], "0xspell", {}, None, id="no events"),
        pytest.param(
            [("0xabcd", date(2026, 4, 3))],
            "0xspell",
            {"0xabcd": ["0xspell"]},
            date(2026, 4, 3),
            id="matching slate",
        ),
        pytest.param(
            [("0xabcd", date(2026, 3, 31))], "0xspell", {"0xabcd": ["0xspell"]}, None, id="event before start date"
        ),
        pytest.param(
            [("0xabcd", date(2026, 4, 30))],
            "0xspell",
            {"0xabcd": ["0xspell"]},
            date(2026, 4, 30),
            id="long after start is still returned, dating decides lateness",
        ),
        pytest.param(
            [("0xabcd", date(2026, 4, 3))], "0xspell", {"0xabcd": ["0xother"]}, None, id="slate lacks the spell"
        ),
        pytest.param(
            [("0xabcd", date(2026, 4, 3))],
            "0xSPELL",
            {"0xabcd": ["0xspell"]},
            date(2026, 4, 3),
            id="address case-insensitive",
        ),
        pytest.param([("0xunknown", date(2026, 4, 3))], "0xspell", {}, None, id="uncached slate contains nothing"),
        pytest.param(
            [("0xabcd", date(2026, 4, 9)), ("0xef01", date(2026, 4, 2))],
            "0xspell",
            {"0xabcd": ["0xspell"], "0xef01": ["0xspell"]},
            date(2026, 4, 2),
            id="earliest qualifying vote wins",
        ),
    ],
)
def test_first_vote_date_for_spell(events, spell_address, slate_cache, expected):
    """Returns the earliest at-or-after-start event whose slate holds the spell, else None."""
    assert (
        onchain._first_vote_date_for_spell(
            events=events,
            spell_address=spell_address,
            start_date=date(2026, 4, 1),
            slate_cache=slate_cache,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# resolve_pending_executive_votes — orchestrator
# ---------------------------------------------------------------------------


def test_resolve_pending_no_op_when_nothing_pending(tmp_path):
    """No RPC traffic and the same mapping back when no cell is Pending (including when there are no spells)."""
    statuses = {(_VOTER_1, "0xspell1"): "Yes", (_VOTER_2, "0xspell1"): "No"}
    sentinel_w3 = MagicMock()

    kwargs = {"w3": sentinel_w3, "cache_path": tmp_path / "slate_cache.json", "known_block_timestamps": {}}

    result = onchain.resolve_pending_executive_votes(statuses, [_spell("0xspell1", date(2026, 4, 1))], **kwargs)
    no_spells = onchain.resolve_pending_executive_votes({(_VOTER_1, "0xspell1"): PENDING}, [], **kwargs)

    sentinel_w3.eth.get_block.assert_not_called()
    assert result == statuses
    assert no_spells == {(_VOTER_1, "0xspell1"): PENDING}


def _w3_for_resolver(
    *,
    events: list[dict] | None = None,
    slate_addresses: dict[str, list[str]] | None = None,
) -> MagicMock:
    """Build a w3 mock that returns the given events and resolves slates."""
    w3 = MagicMock()

    contract = MagicMock()
    contract.events.Vote.return_value.get_logs.return_value = events or []

    def _slates_factory(slate_bytes, i):
        slate_hex = "0x" + slate_bytes.hex()
        addresses = (slate_addresses or {}).get(slate_hex, [])
        call = MagicMock()
        if i < len(addresses):
            call.call.return_value = addresses[i]
        else:
            call.call.side_effect = ContractLogicError("oob")
        return call

    contract.functions.slates.side_effect = _slates_factory
    w3.eth.contract.return_value = contract
    w3.eth.block_number = 10_000
    return w3


def _resolve_one(
    tmp_path,
    *,
    spell_addr: str,
    vote_day: date,
    slate: str = "0x" + "ab" * 32,
    slate_addresses: dict[str, list[str]] | None = None,
) -> tuple[dict, dict]:
    """Run the resolver for one Pending cell whose only vote event lands on vote_day.

    The spell goes live Wednesday 2026-04-01, so the deadline is Monday 2026-04-06. Returns (input statuses, output
    statuses) so callers can assert on both.
    """
    spell_start = date(2026, 4, 1)
    statuses = {(_VOTER_1, spell_addr): PENDING}
    w3 = _w3_for_resolver(
        events=[_make_event(slate, 1000)],
        slate_addresses={slate: [spell_addr]} if slate_addresses is None else slate_addresses,
    )
    w3.eth.get_block.return_value = {"timestamp": _ts(vote_day)}
    out = onchain.resolve_pending_executive_votes(
        statuses,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=tmp_path / "slate_cache.json",
        known_block_timestamps={},
    )
    return statuses, out


def test_resolve_pending_flips_cell_when_slate_contains_spell_and_leaves_input_untouched(tmp_path):
    """Happy path: an in-deadline vote for a slate containing the spell. The input mapping is not mutated."""
    spell_addr = "0x" + "11" * 20
    # Spell live Wednesday 2026-04-01; the vote lands 2026-04-03, inside the deadline (Monday 2026-04-06).
    statuses, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3))

    assert out[_VOTER_1, spell_addr] == "Yes"
    assert statuses[_VOTER_1, spell_addr] == PENDING


def test_resolve_pending_late_vote_marked_late(tmp_path):
    """A vote past the 3-business-day deadline resolves to 'Late', not 'Yes'."""
    spell_addr = "0x" + "22" * 20
    # Wednesday 2026-04-01 -> deadline Monday 2026-04-06; 2026-04-07 is one day past it.
    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 7))

    assert out[_VOTER_1, spell_addr] == "Late"


def test_resolve_pending_vote_on_deadline_day_is_on_time(tmp_path):
    """The deadline day itself still counts; a weekend between start and deadline is skipped."""
    spell_addr = "0x" + "24" * 20
    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 6))

    assert out[_VOTER_1, spell_addr] == "Yes"


def test_resolve_pending_leaves_cell_pending_when_no_slate_contains_spell(tmp_path):
    spell_addr = "0x" + "25" * 20
    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3), slate_addresses={})

    assert out[_VOTER_1, spell_addr] == PENDING


def test_resolve_pending_persists_cache_growth(tmp_path):
    """Newly resolved slates are written back to the cache file."""
    spell_addr = "0x" + "33" * 20
    slate = "0x" + "ef" * 32

    _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3), slate=slate)

    written = onchain._load_slate_cache(tmp_path / "slate_cache.json")
    assert written == {slate: [spell_addr]}


def test_resolve_pending_reuses_cached_slate(tmp_path):
    """If the slate is already cached, no contract call is made."""
    spell_addr = "0x" + "44" * 20
    slate = "0x" + "ef" * 32
    cache_path = tmp_path / "slate_cache.json"
    onchain._save_slate_cache({slate: [spell_addr]}, cache_path)

    w3 = _w3_for_resolver(events=[_make_event(slate, 1000)])  # no slate_addresses configured
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}
    out = onchain.resolve_pending_executive_votes(
        {(_VOTER_1, spell_addr): PENDING},
        [_spell(spell_addr, date(2026, 4, 1))],
        w3=w3,
        cache_path=cache_path,
        known_block_timestamps={},
    )

    assert out[_VOTER_1, spell_addr] == "Yes"
    w3.eth.contract.return_value.functions.slates.assert_not_called()


def test_resolve_pending_no_cache_write_when_no_new_slates(tmp_path):
    """If the on-chain events reveal only already-cached slates, the file is left alone."""
    spell_addr = "0x" + "55" * 20
    slate = "0x" + "ef" * 32
    cache_path = tmp_path / "slate_cache.json"
    onchain._save_slate_cache({slate: [spell_addr]}, cache_path)
    original_mtime = cache_path.stat().st_mtime_ns

    w3 = _w3_for_resolver(events=[_make_event(slate, 1000)])
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}
    onchain.resolve_pending_executive_votes(
        {(_VOTER_1, spell_addr): PENDING},
        [_spell(spell_addr, date(2026, 4, 1))],
        w3=w3,
        cache_path=cache_path,
        known_block_timestamps={},
    )

    assert cache_path.stat().st_mtime_ns == original_mtime
