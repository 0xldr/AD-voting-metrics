"""Tests for the on-chain executive-vote verifier.

All w3 calls are mocked — no real RPC traffic. The slate cache uses a
tmp_path file in each test that needs it.
"""

import json
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest
import responses
from hexbytes import HexBytes
from web3.exceptions import ContractLogicError

from ad_voting_metrics.sources import http as http_module
from ad_voting_metrics.sources import sky_executive_onchain as onchain

PENDING = "Pending verification"


@pytest.fixture(autouse=True)
def _reset_session():
    """Clear the cached requests.Session before and after every test in this module."""
    http_module.get_session.cache_clear()
    yield
    http_module.get_session.cache_clear()


def _ts(d: date) -> int:
    """Unix timestamp for midnight UTC on `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _mock_blockscout(block_number: int = 22_000_000) -> None:
    """Register a Blockscout getblocknobytime response for the active `responses` context."""
    responses.add(
        responses.GET,
        onchain.BLOCKSCOUT_API_URL,
        json={"status": "1", "message": "OK", "result": str(block_number)},
    )


# ---------------------------------------------------------------------------
# Slate cache I/O
# ---------------------------------------------------------------------------


def test_load_slate_cache_missing_file_returns_empty(tmp_path):
    assert onchain._load_slate_cache(tmp_path / "nope.json") == {}


def test_load_slate_cache_lowercases_keys_and_addresses(tmp_path):
    cache_path = tmp_path / "slate_cache.json"
    cache_path.write_text(
        json.dumps({
            "0xABCDEF": ["0x1111", "0x2222"],
            "0xBEEF": ["0x3333"],
        })
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


def test_resolve_slate_walks_until_revert():
    w3 = _make_w3_with_slates([
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ])
    out = onchain._resolve_slate(w3, "0xabcd")
    assert out == [
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
        "0x3333333333333333333333333333333333333333",
    ]


def test_resolve_slate_stops_at_zero_address():
    """Some chiefs sentinel-terminate with the zero address rather than reverting."""
    w3 = _make_w3_with_slates([
        "0x1111111111111111111111111111111111111111",
        "0x0000000000000000000000000000000000000000",  # sentinel
        "0xshouldNotReachHere",
    ])
    out = onchain._resolve_slate(w3, "0xabcd")
    assert out == ["0x1111111111111111111111111111111111111111"]


def test_resolve_slate_returns_lowercased_addresses():
    w3 = _make_w3_with_slates(["0xAABBccDDeeFF00112233445566778899AABBCCDD"])
    out = onchain._resolve_slate(w3, "0xabcd")
    assert out == ["0xaabbccddeeff00112233445566778899aabbccdd"]


def test_resolve_slate_safety_cap_on_runaway():
    """MAX_SLATE_LENGTH bounds the walk if the contract never reverts."""
    w3 = _make_w3_with_slates(["0x1111111111111111111111111111111111111111"] * 100)
    out = onchain._resolve_slate(w3, "0xabcd")
    assert len(out) == onchain.MAX_SLATE_LENGTH


# ---------------------------------------------------------------------------
# _block_from_date
# ---------------------------------------------------------------------------


@responses.activate
def test_block_from_date_returns_blockscout_result():
    responses.add(
        responses.GET,
        onchain.BLOCKSCOUT_API_URL,
        json={"status": "1", "message": "OK", "result": "21500000"},
    )
    assert onchain._block_from_date(date(2026, 5, 13)) == 21_500_000


@responses.activate
def test_block_from_date_sends_midnight_utc_timestamp():
    """The function must query by midnight UTC of the target date, with closest=after."""
    responses.add(
        responses.GET,
        onchain.BLOCKSCOUT_API_URL,
        json={"status": "1", "message": "OK", "result": "1"},
    )
    onchain._block_from_date(date(2026, 5, 13))
    request_url = responses.calls[0].request.url
    assert request_url is not None
    assert f"timestamp={_ts(date(2026, 5, 13))}" in request_url
    assert "closest=after" in request_url
    assert "module=block" in request_url
    assert "action=getblocknobytime" in request_url


# ---------------------------------------------------------------------------
# _fetch_vote_events
# ---------------------------------------------------------------------------


_VOTER_1 = "0x" + "01" * 20
_VOTER_2 = "0x" + "02" * 20


def _make_event(slate_hex: str, block_number: int, voter: str = _VOTER_1) -> dict:
    """Build a decoded EventData dict matching what contract.events.Vote.get_logs returns."""
    from web3 import Web3 as _Web3

    return {
        "args": {
            "usr": _Web3.to_checksum_address(voter),
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
# _identify_pending_pairs
# ---------------------------------------------------------------------------


def test_identify_pending_pairs_only_flags_pending_cells():
    df = pd.DataFrame({
        "Delegate Contract": ["0xa", "0xb", "0xc"],
        "0xspell1": ["Yes", PENDING, "No"],
        "0xspell2": [PENDING, PENDING, "Yes"],
        "0xspell3": ["Yes", "Yes", "Yes"],
    })
    out = onchain._identify_pending_pairs(df, ["0xspell1", "0xspell2", "0xspell3"])
    assert out == {"0xspell1": [1], "0xspell2": [0, 1]}


def test_identify_pending_pairs_skips_missing_columns():
    df = pd.DataFrame({"Delegate Contract": ["0xa"], "0xspell1": [PENDING]})
    out = onchain._identify_pending_pairs(df, ["0xspell1", "0xspell_missing"])
    assert "0xspell_missing" not in out
    assert out == {"0xspell1": [0]}


# ---------------------------------------------------------------------------
# _delegate_voted_for_spell
# ---------------------------------------------------------------------------


def _slate_with(spell_addr: str) -> dict:
    return {"0xabcd": [spell_addr]}


def test_delegate_voted_for_spell_no_events():
    assert (
        onchain._delegate_voted_for_spell(
            events=[],
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache={},
        )
        is False
    )


def test_delegate_voted_for_spell_event_in_window_with_matching_slate():
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xabcd", date(2026, 4, 3))],
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache=_slate_with("0xspell"),
        )
        is True
    )


def test_delegate_voted_for_spell_event_before_start_date_ignored():
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xabcd", date(2026, 3, 31))],
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache=_slate_with("0xspell"),
        )
        is False
    )


def test_delegate_voted_for_spell_event_past_deadline_ignored():
    """7-day cutoff: an otherwise-matching vote on day 8+ does not count."""
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xabcd", date(2026, 4, 9))],  # 8 days after start
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache=_slate_with("0xspell"),
        )
        is False
    )


def test_delegate_voted_for_spell_slate_missing_spell_ignored():
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xabcd", date(2026, 4, 3))],
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache={"0xabcd": ["0xother"]},
        )
        is False
    )


def test_delegate_voted_for_spell_address_case_insensitive():
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xabcd", date(2026, 4, 3))],
            spell_address="0xSPELL",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache={"0xabcd": ["0xspell"]},
        )
        is True
    )


def test_delegate_voted_for_spell_unknown_slate_ignored():
    """A slate not in the cache is treated as containing nothing."""
    assert (
        onchain._delegate_voted_for_spell(
            events=[("0xunknown", date(2026, 4, 3))],
            spell_address="0xspell",
            start_date=date(2026, 4, 1),
            deadline=date(2026, 4, 8),
            slate_cache={},
        )
        is False
    )


# ---------------------------------------------------------------------------
# resolve_pending_executive_votes — orchestrator
# ---------------------------------------------------------------------------


def _make_df(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """(delegate_contract, spell1_status, spell2_status) per row."""
    return pd.DataFrame({
        "Delegate Contract": [r[0] for r in rows],
        "0xspell1": [r[1] for r in rows],
        "0xspell2": [r[2] for r in rows],
    })


def _spell(addr: str, start: date) -> dict:
    return {"address": addr, "startDate": start, "title": "x"}


def test_resolve_pending_no_op_when_spell_info_empty():
    df = pd.DataFrame({"Delegate Contract": ["0xa"], "0xspell1": [PENDING]})
    result = onchain.resolve_pending_executive_votes(df, spell_info=[])
    assert result is df  # mutated in place; nothing to do


def test_resolve_pending_no_op_when_no_pending_cells():
    df = _make_df([("0xa", "Yes", "No")])
    spell_info = [_spell("0xspell1", date(2026, 4, 1))]
    # No w3 should be needed; pass a sentinel and assert it isn't touched.
    sentinel_w3 = MagicMock()
    result = onchain.resolve_pending_executive_votes(df, spell_info, w3=sentinel_w3)
    sentinel_w3.eth.get_block.assert_not_called()
    assert result.equals(df)


def test_resolve_pending_skips_when_rpc_url_missing(monkeypatch, caplog):
    monkeypatch.delenv("SKY_RPC_URL", raising=False)
    df = _make_df([("0xa", PENDING, "Yes")])
    spell_info = [_spell("0xspell1", date(2026, 4, 1))]
    with caplog.at_level("WARNING"):
        result = onchain.resolve_pending_executive_votes(df, spell_info)
    assert "SKY_RPC_URL is not set" in caplog.text
    assert result.loc[0, "0xspell1"] == PENDING


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
    return w3


@responses.activate
def test_resolve_pending_flips_cell_when_slate_contains_spell(tmp_path):
    """Happy path: a delegate's in-window vote for a slate containing the spell."""
    _mock_blockscout()
    spell_addr = "0x" + "11" * 20
    spell_start = date(2026, 4, 1)
    df = pd.DataFrame({
        "Delegate Contract": [_VOTER_1],
        spell_addr: [PENDING],
    })

    slate = "0x" + "ab" * 32
    events = [_make_event(slate, 1000)]
    w3 = _w3_for_resolver(events=events, slate_addresses={slate: [spell_addr]})
    # The log's block falls on 2026-04-03 (within 7-day window).
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}

    result = onchain.resolve_pending_executive_votes(
        df,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=tmp_path / "slate_cache.json",
    )
    assert result.loc[0, spell_addr] == "Yes"


@responses.activate
def test_resolve_pending_late_vote_stays_pending(tmp_path):
    """A vote 8 days after spell start does not flip the cell."""
    _mock_blockscout()
    spell_addr = "0x" + "22" * 20
    spell_start = date(2026, 4, 1)
    df = pd.DataFrame({
        "Delegate Contract": [_VOTER_1],
        spell_addr: [PENDING],
    })

    slate = "0x" + "cd" * 32
    events = [_make_event(slate, 1000)]
    w3 = _w3_for_resolver(events=events, slate_addresses={slate: [spell_addr]})
    # 2026-04-09 — one day past the 7-day window.
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 9))}

    result = onchain.resolve_pending_executive_votes(
        df,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=tmp_path / "slate_cache.json",
    )
    assert result.loc[0, spell_addr] == PENDING


@responses.activate
def test_resolve_pending_persists_cache_growth(tmp_path):
    """Newly resolved slates are written back to the cache file."""
    _mock_blockscout()
    spell_addr = "0x" + "33" * 20
    spell_start = date(2026, 4, 1)
    df = pd.DataFrame({
        "Delegate Contract": [_VOTER_1],
        spell_addr: [PENDING],
    })

    slate = "0x" + "ef" * 32
    events = [_make_event(slate, 1000)]
    w3 = _w3_for_resolver(events=events, slate_addresses={slate: [spell_addr]})
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}

    cache_path = tmp_path / "slate_cache.json"
    onchain.resolve_pending_executive_votes(
        df,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=cache_path,
    )

    assert cache_path.exists()
    written = onchain._load_slate_cache(cache_path)
    assert slate.lower() in written
    assert written[slate.lower()] == [spell_addr]


@responses.activate
def test_resolve_pending_reuses_cached_slate(tmp_path):
    """If the slate is already cached, no contract call is made."""
    _mock_blockscout()
    spell_addr = "0x" + "44" * 20
    spell_start = date(2026, 4, 1)
    slate = "0x" + "ef" * 32
    df = pd.DataFrame({
        "Delegate Contract": [_VOTER_1],
        spell_addr: [PENDING],
    })

    cache_path = tmp_path / "slate_cache.json"
    onchain._save_slate_cache({slate.lower(): [spell_addr]}, cache_path)

    events = [_make_event(slate, 1000)]
    w3 = _w3_for_resolver(events=events)  # no slate_addresses configured
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}

    result = onchain.resolve_pending_executive_votes(
        df,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=cache_path,
    )
    # Should still flip the cell using the cached slate.
    assert result.loc[0, spell_addr] == "Yes"
    # contract.functions.slates was never invoked.
    w3.eth.contract.return_value.functions.slates.assert_not_called()


@responses.activate
def test_resolve_pending_no_cache_write_when_no_new_slates(tmp_path):
    """If the on-chain events reveal only already-cached slates, the file is left alone."""
    _mock_blockscout()
    spell_addr = "0x" + "55" * 20
    spell_start = date(2026, 4, 1)
    slate = "0x" + "ef" * 32
    df = pd.DataFrame({
        "Delegate Contract": [_VOTER_1],
        spell_addr: [PENDING],
    })

    cache_path = tmp_path / "slate_cache.json"
    onchain._save_slate_cache({slate.lower(): [spell_addr]}, cache_path)
    original_mtime = cache_path.stat().st_mtime_ns

    events = [_make_event(slate, 1000)]
    w3 = _w3_for_resolver(events=events)
    w3.eth.get_block.return_value = {"timestamp": _ts(date(2026, 4, 3))}

    onchain.resolve_pending_executive_votes(
        df,
        [_spell(spell_addr, spell_start)],
        w3=w3,
        cache_path=cache_path,
    )
    # File untouched (mtime unchanged).
    assert cache_path.stat().st_mtime_ns == original_mtime
