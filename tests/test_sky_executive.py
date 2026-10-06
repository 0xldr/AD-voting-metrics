"""Tests for sources.sky_executive: the vote.sky.money listing, seed statuses, and on-chain settlement (w3 mocked)."""

import logging
from datetime import date
from unittest.mock import MagicMock

import pytest
import responses
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import ContractLogicError

from ad_voting_metrics.ballots import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import sky_executive
from ad_voting_metrics.sources.json_cache import load_json_cache, save_json_cache
from tests.helpers import ADDR_A, ADDR_B, delegate, request_urls, ts

_SPELL_1 = "0x" + "11" * 20
_SPELL_2 = "0x" + "22" * 20
_LIVE = date(2026, 4, 5)
_SLATE = "0x" + "ab" * 32


def _spell(address: str, start: date = _LIVE) -> Ballot:
    return Ballot(id=address, start=start, end=None, title="Test spell")


def test_spell_statuses_has_an_entry_per_delegate_per_spell():
    out = sky_executive.spell_statuses([_spell(_SPELL_1), _spell(_SPELL_2)], [delegate()], {(ADDR_A, _LIVE): 1000.0})

    assert set(out) == {(ADDR_A, _SPELL_1), (ADDR_A, _SPELL_2)}


@pytest.mark.parametrize(
    ("aligned_from", "aligned_to", "sky", "expected"),
    [
        pytest.param(date(2024, 1, 1), None, 1000.0, "Pending verification", id="sky on the live day awaits timing"),
        pytest.param(date(2024, 1, 1), None, 0.0, "No Delegated SKY", id="no sky on the live day"),
        pytest.param(date(2026, 5, 1), None, 1000.0, "Not Started", id="aligned after the spell went live"),
        pytest.param(date(2024, 1, 1), date(2026, 4, 4), 1000.0, "Exited", id="exited the day before it went live"),
        pytest.param(date(2024, 1, 1), _LIVE, 1000.0, "Pending verification", id="exited on the live day still counts"),
    ],
)
def test_spell_statuses_seed(aligned_from, aligned_to, sky, expected):
    delegates = [delegate(start=aligned_from, end=aligned_to)]

    out = sky_executive.spell_statuses([_spell(_SPELL_1)], delegates, {(ADDR_A, _LIVE): sky})

    assert out[ADDR_A, _SPELL_1] == expected


def test_spell_statuses_empty_spells_returns_empty():
    assert sky_executive.spell_statuses([], [delegate()], {}) == {}


def _executive_dict(address: str, date_iso: str, title: str = "Test spell") -> dict:
    return {"address": address, "date": date_iso, "title": title}


def _add_page(executives: list[dict]) -> None:
    responses.add(responses.GET, sky_executive.SKY_EXECUTIVE_URL, json=executives)


@responses.activate
def test_fetch_spells_for_period_filters_to_period_and_stops_at_older_spell():
    """Newest-first listing: later spells skipped, the in-period one kept, paging stops at the first older one."""
    _add_page(
        [
            _executive_dict("0xCCCC000000000000000000000000000000000003", "2025-06-10T00:00:00Z", "After"),
            _executive_dict("0xAAAA000000000000000000000000000000000001", "2025-04-10T00:00:00Z", "In window"),
            _executive_dict("0xBBBB000000000000000000000000000000000002", "2025-02-10T00:00:00Z", "Before"),
        ]
    )

    result = sky_executive.fetch_spells_for_period(MonthPeriod(2025, 4))

    assert result == [
        Ballot(id="0xaaaa000000000000000000000000000000000001", start=date(2025, 4, 10), end=None, title="In window")
    ]
    assert len(responses.calls) == 1


@responses.activate
def test_fetch_spells_for_period_advances_start_until_empty():
    """The `start` query advances by SKY_EXECUTIVES_PAGE_SIZE until the API returns []."""
    _add_page([_executive_dict("0xspell0000000000000000000000000000000002", "2025-04-22T00:00:00Z")])
    _add_page([_executive_dict("0xspell0000000000000000000000000000000001", "2025-04-05T00:00:00Z")])
    _add_page([])

    result = sky_executive.fetch_spells_for_period(MonthPeriod(2025, 4))

    assert len(result) == 2
    page_size = sky_executive.SKY_EXECUTIVES_PAGE_SIZE
    urls = request_urls()
    assert len(urls) == 3
    assert "start=0" in urls[0]
    assert f"start={page_size}" in urls[1]
    assert f"start={page_size * 2}" in urls[2]


@responses.activate
def test_fetch_spells_for_period_empty_first_page_returns_empty():
    _add_page([])

    assert sky_executive.fetch_spells_for_period(MonthPeriod(2025, 4)) == []
    assert len(responses.calls) == 1


def _fake_chain(genesis_ts: int, seconds_per_block: int):
    """A `get_block` stand-in for a chain with evenly spaced blocks starting at `genesis_ts`."""

    def get_block(number: int) -> dict[str, int]:
        return {"timestamp": genesis_ts + number * seconds_per_block}

    return get_block


def _make_event(slate_hex: str, block_number: int, voter: str = ADDR_A) -> dict:
    """Build a decoded EventData dict matching what contract.events.Vote.get_logs returns."""
    return {
        "args": {
            "usr": Web3.to_checksum_address(voter),
            "slate": HexBytes(bytes.fromhex(slate_hex.removeprefix("0x").zfill(64))),
        },
        "blockNumber": block_number,
    }


def _w3(*, events: list[dict] | None = None, slates: dict[str, list[str]] | None = None) -> MagicMock:
    """A w3 whose chief returns `events` from Vote.get_logs and walks `slates` (slate hex -> addresses) via slates(i).

    Past a slate's last address the call reverts with ContractLogicError, as the real contract does.
    """
    w3 = MagicMock()
    w3.batch_requests.side_effect = ValueError("no batch support")
    contract = w3.eth.contract.return_value
    contract.events.Vote.return_value.get_logs.return_value = events or []

    def slates_at(slate_bytes, i):
        addresses = (slates or {}).get(Web3.to_hex(slate_bytes), [])
        call = MagicMock()
        if i < len(addresses):
            call.call.return_value = addresses[i]
        else:
            call.call.side_effect = ContractLogicError("array out of bounds")
        return call

    contract.functions.slates.side_effect = slates_at
    w3.eth.block_number = 10_000
    return w3


def test_resolve_slate_walks_until_revert_and_lowercases():
    w3 = _w3(
        slates={
            "0xabcd": [
                "0x1111111111111111111111111111111111111111",
                "0xAABBccDDeeFF00112233445566778899AABBCCDD",
                "0x3333333333333333333333333333333333333333",
            ]
        }
    )

    assert sky_executive._resolve_slate(w3, "0xabcd") == [
        "0x1111111111111111111111111111111111111111",
        "0xaabbccddeeff00112233445566778899aabbccdd",
        "0x3333333333333333333333333333333333333333",
    ]


def test_resolve_slate_stops_at_zero_address():
    """Some chiefs sentinel-terminate with the zero address rather than reverting."""
    w3 = _w3(
        slates={
            "0xabcd": [
                "0x1111111111111111111111111111111111111111",
                "0x0000000000000000000000000000000000000000",
                "0xshouldNotReachHere",
            ]
        }
    )

    assert sky_executive._resolve_slate(w3, "0xabcd") == ["0x1111111111111111111111111111111111111111"]


def test_resolve_slate_safety_cap_on_runaway():
    w3 = _w3(slates={"0xabcd": ["0x1111111111111111111111111111111111111111"] * 100})

    assert len(sky_executive._resolve_slate(w3, "0xabcd")) == sky_executive.MAX_SLATE_LENGTH


def test_resolve_slate_stops_on_a_decode_error_and_notes_it(caplog):
    """Anything other than the contract's own out-of-bounds revert ends the walk with a debug record of why."""
    w3 = _w3()
    slates = w3.eth.contract.return_value.functions.slates
    slates.side_effect = None
    slates.return_value.call.side_effect = ValueError("undecodable return data")

    with caplog.at_level(logging.DEBUG, logger="ad_voting_metrics"):
        assert sky_executive._resolve_slate(w3, "0xabcd") == []

    assert "walk stopped at index 0 on ValueError" in caplog.text


def test_block_from_date_finds_first_block_at_or_after_midnight():
    """Binary search converges on the first block whose timestamp >= midnight UTC of the target date."""
    genesis_ts = ts(date(2026, 5, 1))
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 200_000
    mock_w3.eth.get_block.side_effect = _fake_chain(genesis_ts, seconds_per_block=12)

    result = sky_executive._block_from_date(mock_w3, date(2026, 5, 13), {}, to_block=200_000)

    # 12 days * 86400 s / 12 s per block — timestamp lands exactly on midnight, so that block is included.
    assert result == 12 * 86400 // 12
    # Binary search, not a linear scan: far fewer calls than the block range.
    assert mock_w3.eth.get_block.call_count < 25


def test_block_from_date_returns_first_block_when_chain_starts_after_target():
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 1_000
    mock_w3.eth.get_block.side_effect = _fake_chain(ts(date(2026, 6, 1)), seconds_per_block=1)

    assert sky_executive._block_from_date(mock_w3, date(2026, 5, 13), {}, to_block=1_000) == 1


def test_block_from_date_uses_latest_known_block_before_target_without_rpc():
    """A cached block dated before the target is returned as-is; blocks at or after the target are ignored."""
    mock_w3 = MagicMock()
    target = date(2026, 5, 13)
    known = {100: ts(date(2026, 5, 10)), 250: ts(target) - 60, 300: ts(target) + 60}

    assert sky_executive._block_from_date(mock_w3, target, known, to_block=1_000) == 250
    mock_w3.eth.get_block.assert_not_called()


def test_fetch_vote_events_makes_one_get_logs_call_for_n_voters():
    """The bulk fetch passes voters as an OR'd argument filter, not one call per voter."""
    w3 = _w3()

    sky_executive._fetch_vote_events(w3, {ADDR_A, ADDR_B}, 500, 9_000, {})

    get_logs = w3.eth.contract.return_value.events.Vote.return_value.get_logs
    assert get_logs.call_count == 1
    assert len(get_logs.call_args.kwargs["argument_filters"]["usr"]) == 2
    assert (get_logs.call_args.kwargs["from_block"], get_logs.call_args.kwargs["to_block"]) == (500, 9_000)


def test_fetch_vote_events_groups_by_voter_with_empty_lists_for_silent_voters():
    w3 = _w3(events=[_make_event("0xabcd", 1000, voter=ADDR_A)])
    w3.eth.get_block.return_value = {"timestamp": ts(date(2026, 5, 20))}

    out = sky_executive._fetch_vote_events(w3, {ADDR_A, ADDR_B}, 500, 9_000, {})

    assert out == {ADDR_A: [("0x" + "abcd".zfill(64), date(2026, 5, 20))], ADDR_B: []}


def test_fetch_vote_events_empty_voter_set_skips_rpc():
    w3 = _w3()

    assert sky_executive._fetch_vote_events(w3, set(), 500, 9_000, {}) == {}
    w3.eth.contract.assert_not_called()


def test_fetch_vote_events_caches_block_timestamps():
    """Same block number across multiple logs => one get_block call."""
    w3 = _w3(
        events=[
            _make_event("0xabcd", 1000, voter=ADDR_A),
            _make_event("0xbeef", 1000, voter=ADDR_A),
            _make_event("0xcafe", 1000, voter=ADDR_B),
        ]
    )
    w3.eth.get_block.return_value = {"timestamp": ts(date(2026, 5, 20))}

    sky_executive._fetch_vote_events(w3, {ADDR_A, ADDR_B}, 500, 9_000, {})

    assert w3.eth.get_block.call_count == 1


def test_fetch_vote_events_dates_events_from_known_timestamps_without_rpc():
    """Blocks the delegation cache already dated need no get_block call."""
    w3 = _w3(events=[_make_event("0xabcd", 1000, voter=ADDR_A)])

    out = sky_executive._fetch_vote_events(w3, {ADDR_A}, 500, 9_000, {1000: ts(date(2026, 5, 20))})

    assert out[ADDR_A] == [("0x" + "abcd".zfill(64), date(2026, 5, 20))]
    w3.eth.get_block.assert_not_called()


def test_pending_pairs_only_flags_pending_cells_for_known_spells():
    statuses = {
        ("0xa", "0xspell1"): "Yes",
        ("0xb", "0xspell1"): "Pending verification",
        ("0xc", "0xspell1"): "No",
        ("0xa", "0xspell2"): "Pending verification",
        ("0xb", "0xspell2"): "Pending verification",
        ("0xa", "0xspell_unknown"): "Pending verification",
    }
    spells = [_spell("0xspell1", date(2026, 4, 1)), _spell("0xspell2", date(2026, 4, 8))]

    assert sky_executive._pending_pairs(statuses, spells) == {
        ("0xb", "0xspell1"),
        ("0xa", "0xspell2"),
        ("0xb", "0xspell2"),
    }


@pytest.mark.parametrize(
    ("events", "slate_cache", "expected"),
    [
        pytest.param([], {}, None, id="no events"),
        pytest.param([("0xabcd", date(2026, 4, 3))], {"0xabcd": ["0xspell"]}, date(2026, 4, 3), id="matching slate"),
        pytest.param([("0xabcd", date(2026, 3, 31))], {"0xabcd": ["0xspell"]}, None, id="event before start date"),
        pytest.param(
            [("0xabcd", date(2026, 4, 30))],
            {"0xabcd": ["0xspell"]},
            date(2026, 4, 30),
            id="long after start is still returned, dating decides lateness",
        ),
        pytest.param([("0xabcd", date(2026, 4, 3))], {"0xabcd": ["0xother"]}, None, id="slate lacks the spell"),
        pytest.param([("0xunknown", date(2026, 4, 3))], {}, None, id="uncached slate contains nothing"),
        pytest.param(
            [("0xabcd", date(2026, 4, 9)), ("0xef01", date(2026, 4, 2))],
            {"0xabcd": ["0xspell"], "0xef01": ["0xspell"]},
            date(2026, 4, 2),
            id="earliest qualifying vote wins",
        ),
    ],
)
def test_first_vote_date_for_spell(events, slate_cache, expected):
    """Returns the earliest at-or-after-start event whose slate holds the spell, else None."""
    result = sky_executive._first_vote_date_for_spell(
        events=events, spell_address="0xspell", start_date=date(2026, 4, 1), slate_cache=slate_cache
    )

    assert result == expected


def test_resolve_pending_no_op_when_nothing_pending(tmp_path):
    """No RPC traffic and the same mapping back when no cell is Pending (including when there are no spells)."""
    statuses = {(ADDR_A, "0xspell1"): "Yes", (ADDR_B, "0xspell1"): "No"}
    sentinel_w3 = MagicMock()
    kwargs = {"w3": sentinel_w3, "cache_path": tmp_path / "slate_cache.json", "known_block_timestamps": {}}

    roster = [delegate(address=ADDR_A), delegate("Bob", ADDR_B)]

    result = sky_executive.resolve_pending_executive_votes(
        statuses, [_spell("0xspell1", date(2026, 4, 1))], roster, **kwargs
    )
    no_spells = sky_executive.resolve_pending_executive_votes(
        {(ADDR_A, "0xspell1"): "Pending verification"}, [], roster, **kwargs
    )

    sentinel_w3.eth.get_block.assert_not_called()
    assert result == statuses
    assert no_spells == {(ADDR_A, "0xspell1"): "Pending verification"}


def _resolve_one(
    tmp_path,
    *,
    spell_addr: str,
    vote_day: date,
    slates: dict[str, list[str]] | None = None,
    end: date | None = None,
) -> tuple[dict, dict]:
    """Run the resolver for one Pending cell whose only vote event lands on vote_day.

    The spell goes live Wednesday 2026-04-01, so the deadline is Monday 2026-04-06. By default the slate voted for
    contains the spell and the delegate has not exited. Returns (input statuses, output statuses) so callers can
    assert on both.
    """
    statuses = {(ADDR_A, spell_addr): "Pending verification"}
    w3 = _w3(events=[_make_event(_SLATE, 1000)], slates={_SLATE: [spell_addr]} if slates is None else slates)
    w3.eth.get_block.return_value = {"timestamp": ts(vote_day)}
    out = sky_executive.resolve_pending_executive_votes(
        statuses,
        [_spell(spell_addr, date(2026, 4, 1))],
        [delegate(address=ADDR_A, end=end)],
        w3=w3,
        cache_path=tmp_path / "slate_cache.json",
        known_block_timestamps={},
    )
    return statuses, out


def test_resolve_pending_flips_cell_when_slate_contains_spell_and_leaves_input_untouched(tmp_path):
    """A vote on 2026-04-03, inside the deadline of Monday 2026-04-06, resolves to Yes without mutating the input."""
    spell_addr = "0x" + "11" * 20

    statuses, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3))

    assert out[ADDR_A, spell_addr] == "Yes"
    assert statuses[ADDR_A, spell_addr] == "Pending verification"


def test_resolve_pending_late_vote_marked_late(tmp_path):
    """2026-04-07 is one day past the Monday 2026-04-06 deadline."""
    spell_addr = "0x" + "22" * 20

    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 7))

    assert out[ADDR_A, spell_addr] == "Late"


def test_resolve_pending_vote_on_deadline_day_is_on_time(tmp_path):
    spell_addr = "0x" + "24" * 20

    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 6))

    assert out[ADDR_A, spell_addr] == "Yes"


@pytest.mark.parametrize(
    ("end", "vote_day", "slates", "expected"),
    [
        pytest.param(date(2026, 4, 2), date(2026, 4, 2), None, "Yes", id="voted on the last aligned day"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 3), None, "Exited", id="voted after leaving, before deadline"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 3), {}, "Exited", id="left before the deadline, never voted"),
        pytest.param(
            date(2026, 4, 6), date(2026, 4, 3), {}, "Pending verification", id="left on deadline day, no vote"
        ),
        pytest.param(date(2026, 4, 30), date(2026, 4, 7), None, "Late", id="left after the deadline, voted late"),
    ],
)
def test_resolve_pending_for_a_delegate_who_exits(tmp_path, end, vote_day, slates, expected):
    """Only votes cast while aligned count; leaving before the deadline without one is Exited, not Pending."""
    spell_addr = "0x" + "26" * 20

    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=vote_day, slates=slates, end=end)

    assert out[ADDR_A, spell_addr] == expected


def test_resolve_pending_leaves_cell_pending_when_no_slate_contains_spell(tmp_path):
    spell_addr = "0x" + "25" * 20

    _, out = _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3), slates={})

    assert out[ADDR_A, spell_addr] == "Pending verification"


def test_resolve_pending_persists_cache_growth(tmp_path):
    spell_addr = "0x" + "33" * 20

    _resolve_one(tmp_path, spell_addr=spell_addr, vote_day=date(2026, 4, 3))

    assert load_json_cache(tmp_path / "slate_cache.json") == {_SLATE: [spell_addr]}


def test_resolve_pending_reuses_cached_slate_without_contract_calls_or_rewrite(tmp_path):
    spell_addr = "0x" + "44" * 20
    cache_path = tmp_path / "slate_cache.json"
    save_json_cache({_SLATE: [spell_addr]}, cache_path)
    original_mtime = cache_path.stat().st_mtime_ns
    w3 = _w3(events=[_make_event(_SLATE, 1000)])
    w3.eth.get_block.return_value = {"timestamp": ts(date(2026, 4, 3))}

    out = sky_executive.resolve_pending_executive_votes(
        {(ADDR_A, spell_addr): "Pending verification"},
        [_spell(spell_addr, date(2026, 4, 1))],
        [delegate(address=ADDR_A)],
        w3=w3,
        cache_path=cache_path,
        known_block_timestamps={},
    )

    assert out[ADDR_A, spell_addr] == "Yes"
    w3.eth.contract.return_value.functions.slates.assert_not_called()
    assert cache_path.stat().st_mtime_ns == original_mtime
