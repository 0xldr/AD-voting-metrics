"""Tests for sources.delegation — on-chain event replay and daily totals."""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest
from eth_typing import HexStr
from web3 import Web3
from web3.exceptions import Web3RPCError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import delegation
from tests.helpers import ADDR_A, ADDR_B, delegate, ts


def test_fetch_event_logs_merges_topics_and_signs_amounts():
    """One OR-filtered getLogs call returns both event types; Free amounts come back negated."""
    contract = "0x" + "c" * 40
    from_block = delegation.V3_FACTORY_BLOCK
    lock_log = {
        "address": contract,
        "blockNumber": from_block + 1,
        "data": Web3.to_bytes(100),
        "topics": [Web3.to_bytes(hexstr=HexStr(delegation.LOCK_TOPIC))],
    }
    free_log = {
        "address": contract,
        "blockNumber": from_block + 2,
        "data": Web3.to_bytes(40),
        "topics": [Web3.to_bytes(hexstr=HexStr(delegation.FREE_TOPIC))],
    }
    mock_w3 = MagicMock()
    mock_w3.eth.get_logs.return_value = [lock_log, free_log]

    events, new_blocks = delegation._fetch_event_logs(mock_w3, [contract], from_block, from_block + 10)

    assert events[contract] == [(from_block + 1, 100), (from_block + 2, -40)]
    assert new_blocks == {from_block + 1, from_block + 2}
    mock_w3.eth.get_logs.assert_called_once()
    params = mock_w3.eth.get_logs.call_args.args[0]
    assert params["topics"] == [[delegation.LOCK_TOPIC, delegation.FREE_TOPIC]]


def _lock_log(contract: str, block: int, wad: int) -> dict:
    return {
        "address": contract,
        "blockNumber": block,
        "data": Web3.to_bytes(wad),
        "topics": [Web3.to_bytes(hexstr=HexStr(delegation.LOCK_TOPIC))],
    }


def test_sync_events_second_run_starts_after_last_synced_block_and_appends(tmp_path):
    """A follow-up sync fetches only new blocks and extends the cached events rather than replacing them."""
    cache_path = tmp_path / "delegation_cache.json"
    contract = "0x" + "c" * 40
    factory = delegation.V3_FACTORY_BLOCK
    first_head = factory + 1_000 + delegation.FINALITY_BLOCKS

    def fake_block(number: int) -> dict[str, int]:
        return {"timestamp": 1_700_000_000 + number}

    w3 = MagicMock()
    w3.batch_requests.side_effect = ValueError("no batch support")
    w3.eth.get_block.side_effect = fake_block
    w3.eth.block_number = first_head
    w3.eth.get_logs.return_value = [_lock_log(contract, factory + 10, 100)]

    first = delegation.sync_events(w3, [contract], cache_path=cache_path, rebuild=False)

    assert w3.eth.get_logs.call_args.args[0]["fromBlock"] == factory
    assert first.last_synced_block == factory + 1_000
    assert cache_path.exists()

    w3.eth.block_number = first_head + 500
    w3.eth.get_logs.return_value = [_lock_log(contract, first_head + 100, 50)]

    second = delegation.sync_events(w3, [contract], cache_path=cache_path, rebuild=False)

    assert w3.eth.get_logs.call_args.args[0]["fromBlock"] == factory + 1_001
    assert second.events[contract] == [(factory + 10, 100), (first_head + 100, 50)]
    assert second.last_synced_block == factory + 1_500
    assert delegation.DelegationCache.load(cache_path).events[contract] == second.events[contract]


def test_sync_events_skips_fetch_when_cache_is_current(tmp_path):
    """When last_synced_block already reaches the safe head, no getLogs call is made and the cache is untouched."""
    cache_path = tmp_path / "delegation_cache.json"
    contract = "0x" + "c" * 40
    synced_to = delegation.V3_FACTORY_BLOCK + 500
    cache_path.write_text(
        json.dumps({"last_synced_block": synced_to, "events": {contract: []}, "block_timestamps": {}})
    )
    mtime = cache_path.stat().st_mtime_ns

    w3 = MagicMock()
    w3.eth.block_number = synced_to + delegation.FINALITY_BLOCKS  # safe head == synced_to

    out = delegation.sync_events(w3, [contract], cache_path=cache_path, rebuild=False)

    w3.eth.get_logs.assert_not_called()
    assert out.last_synced_block == synced_to
    assert cache_path.stat().st_mtime_ns == mtime


def test_sync_events_backfills_contracts_new_to_the_roster(tmp_path):
    """A contract absent from the cache is fetched from the factory block first, then joins the shared tail sync."""
    cache_path = tmp_path / "delegation_cache.json"
    known, newcomer = "0x" + "c" * 40, "0x" + "d" * 40
    factory = delegation.V3_FACTORY_BLOCK
    synced_to = factory + 500
    delegation.DelegationCache(last_synced_block=synced_to, events={known: [(factory + 10, 100)]}).save(cache_path)

    def get_logs(params: dict) -> list:
        if params["fromBlock"] == factory:
            return [_lock_log(newcomer, factory + 20, 7)]
        return [_lock_log(newcomer, synced_to + 5, 3)]

    w3 = MagicMock()
    w3.batch_requests.side_effect = ValueError("no batch support")
    w3.eth.get_block.return_value = {"timestamp": 1}
    w3.eth.block_number = synced_to + 100 + delegation.FINALITY_BLOCKS
    w3.eth.get_logs.side_effect = get_logs

    out = delegation.sync_events(w3, [known, newcomer], cache_path=cache_path, rebuild=False)

    backfill, tail = (c.args[0] for c in w3.eth.get_logs.call_args_list)
    assert (backfill["fromBlock"], backfill["toBlock"]) == (factory, synced_to)
    assert backfill["address"] == [Web3.to_checksum_address(newcomer)]
    assert (tail["fromBlock"], tail["toBlock"]) == (synced_to + 1, synced_to + 100)
    assert len(tail["address"]) == 2
    assert out.events[newcomer] == [(factory + 20, 7), (synced_to + 5, 3)]
    assert out.events[known] == [(factory + 10, 100)]
    assert out.last_synced_block == synced_to + 100


def test_sync_events_backfills_a_newcomer_even_when_the_chain_has_nothing_new(tmp_path):
    cache_path = tmp_path / "delegation_cache.json"
    newcomer = "0x" + "d" * 40
    factory = delegation.V3_FACTORY_BLOCK
    synced_to = factory + 500
    delegation.DelegationCache(last_synced_block=synced_to).save(cache_path)

    w3 = MagicMock()
    w3.batch_requests.side_effect = ValueError("no batch support")
    w3.eth.get_block.return_value = {"timestamp": 1}
    w3.eth.block_number = synced_to + delegation.FINALITY_BLOCKS
    w3.eth.get_logs.return_value = [_lock_log(newcomer, factory + 20, 7)]

    out = delegation.sync_events(w3, [newcomer], cache_path=cache_path, rebuild=False)

    w3.eth.get_logs.assert_called_once()
    assert out.events[newcomer] == [(factory + 20, 7)]
    assert out.last_synced_block == synced_to
    assert delegation.DelegationCache.load(cache_path).events[newcomer] == [(factory + 20, 7)]


def test_sync_events_rebuild_discards_cache_and_starts_at_factory_block(tmp_path):
    """With rebuild the existing cache is ignored and the sync restarts from V3_FACTORY_BLOCK."""
    cache_path = tmp_path / "delegation_cache.json"
    contract = "0x" + "c" * 40
    factory = delegation.V3_FACTORY_BLOCK
    delegation.DelegationCache(
        last_synced_block=factory + 500, events={contract: [(factory + 10, 100)]}, block_timestamps={factory + 10: 1}
    ).save(cache_path)

    w3 = MagicMock()
    w3.eth.block_number = factory + 1_000 + delegation.FINALITY_BLOCKS
    w3.eth.get_logs.return_value = []

    out = delegation.sync_events(w3, [contract], cache_path=cache_path, rebuild=True)

    assert w3.eth.get_logs.call_args.args[0]["fromBlock"] == factory
    assert out.events == {contract: []}
    assert out.block_timestamps == {}


def test_fetch_event_logs_raises_when_getlogs_fails_at_minimum_chunk():
    """A provider that rejects every range surfaces a RuntimeError instead of looping forever."""
    mock_w3 = MagicMock()
    mock_w3.eth.get_logs.side_effect = Web3RPCError("range too large")
    factory = delegation.V3_FACTORY_BLOCK

    with pytest.raises(RuntimeError, match="minimum chunk size"):
        delegation._fetch_event_logs(mock_w3, ["0x" + "a" * 40], factory, factory + 100_000)


def test_fetch_event_logs_halves_chunk_until_provider_accepts():
    """Rejected ranges are retried at half the size down to MIN_CHUNK_BLOCKS, then the fetch runs to the head."""

    def get_logs(params: dict) -> list:
        if params["toBlock"] - params["fromBlock"] + 1 > delegation.MIN_CHUNK_BLOCKS:
            raise Web3RPCError("range too large")
        return []

    mock_w3 = MagicMock()
    mock_w3.eth.get_logs.side_effect = get_logs
    factory = delegation.V3_FACTORY_BLOCK

    events, new_blocks = delegation._fetch_event_logs(mock_w3, ["0x" + "a" * 40], factory, factory + 4_999)

    assert (events, new_blocks) == ({}, set())
    assert mock_w3.eth.get_logs.call_args.args[0]["toBlock"] == factory + 4_999


def test_contract_cumulative_balances_nets_same_day_events_and_carries_total():
    """Two events on one day collapse to one entry; later days start from the prior running total."""
    day_1, day_3 = date(2026, 4, 1), date(2026, 4, 3)
    timestamps = {1: ts(day_1), 2: ts(day_1) + 3600, 3: ts(day_3)}
    events = [(1, 100), (2, -40), (3, 10)]

    out = delegation._contract_cumulative_balances(events, timestamps, "0xc")

    assert out == {day_1: 60, day_3: 70}


def test_contract_cumulative_balances_raises_when_total_goes_negative():
    timestamps = {1: ts(date(2026, 4, 1))}

    with pytest.raises(ValueError, match="Negative running total"):
        delegation._contract_cumulative_balances([(1, -5)], timestamps, "0xc")


def test_contract_cumulative_balances_rejects_an_event_without_a_cached_timestamp():
    """A balance computed without one of its events would be wrong, so an inconsistent cache fails the run."""
    timestamps = {1: ts(date(2026, 4, 1))}

    with pytest.raises(ValueError, match="Block 2 of contract 0xc has no cached timestamp"):
        delegation._contract_cumulative_balances([(1, 100), (2, 100)], timestamps, "0xc")


_APRIL = MonthPeriod(2026, 4)


def _cache(events: list[tuple[str, date, int]]) -> delegation.DelegationCache:
    """Build a cache from (contract, day, signed SKY amount) events, one synthetic block per event."""
    cache = delegation.DelegationCache()
    for block, (contract, day, sky) in enumerate(events, start=1):
        cache.events.setdefault(contract, []).append((block, Web3.to_wei(sky, "ether")))
        cache.block_timestamps[block] = ts(day)
    return cache


def test_daily_balances_zero_before_first_event_then_carried_forward():
    """One row per day of the month; days before the first event are zero and later days keep the last balance."""
    cache = _cache([(ADDR_A, date(2026, 4, 2), 1500)])

    result = delegation.daily_balances(cache, [delegate("Alice", ADDR_A)], _APRIL)

    assert list(result.columns) == ["contract", "name", "date", "sky", "rank"]
    assert len(result) == 30
    assert set(result["name"]) == {"Alice"}
    by_date = dict(zip(result["date"], result["sky"], strict=True))
    assert by_date[date(2026, 4, 1)] == 0.0
    assert by_date[date(2026, 4, 2)] == 1500.0
    assert by_date[date(2026, 4, 30)] == 1500.0


def test_daily_balances_carries_pre_period_balance_across_whole_period():
    """A delegate whose last Lock/Free predates the period keeps that balance every day of it."""
    cache = _cache([(ADDR_A, date(2026, 3, 15), 1_000_000)])

    result = delegation.daily_balances(cache, [delegate("Alice", ADDR_A)], _APRIL)

    assert set(result["sky"]) == {1_000_000.0}


def test_daily_balances_ranks_delegates_within_each_day_and_sorts_by_date_then_rank():
    cache = _cache([(ADDR_A, date(2026, 4, 1), 500), (ADDR_B, date(2026, 4, 1), 1000), (ADDR_A, date(2026, 4, 2), 600)])

    result = delegation.daily_balances(cache, [delegate("Alice", ADDR_A), delegate("Bob", ADDR_B)], _APRIL)

    first_two_days = result[result["date"] <= date(2026, 4, 2)]
    assert list(
        zip(first_two_days["date"], first_two_days["name"], first_two_days["sky"], first_two_days["rank"], strict=True)
    ) == [
        (date(2026, 4, 1), "Bob", 1000.0, 1),
        (date(2026, 4, 1), "Alice", 500.0, 2),
        (date(2026, 4, 2), "Alice", 1100.0, 1),
        (date(2026, 4, 2), "Bob", 1000.0, 2),
    ]


def test_daily_balances_breaks_rank_ties_by_roster_order():
    cache = _cache([(ADDR_A, date(2026, 4, 1), 100), (ADDR_B, date(2026, 4, 1), 100)])

    result = delegation.daily_balances(cache, [delegate("Alice", ADDR_A), delegate("Bob", ADDR_B)], _APRIL)

    first_day = result[result["date"] == date(2026, 4, 1)]
    assert list(zip(first_day["name"], first_day["rank"], strict=True)) == [("Alice", 1), ("Bob", 2)]


def test_delegation_cache_load_normalises_legacy_string_keys_and_wads(tmp_path):
    """Older cache files stored block keys and wads as strings; load yields ints either way."""
    cache_path = tmp_path / "delegation_cache.json"
    cache_path.write_text(
        json.dumps(
            {
                "last_synced_block": 22500000,
                "events": {"0xABC": [[22400000, "100"], [22400500, "-40"]]},
                "block_timestamps": {"22400000": 1_700_000_000, "22400500": 1_700_006_000},
            }
        )
    )

    cache = delegation.DelegationCache.load(cache_path)

    assert cache.last_synced_block == 22500000
    assert cache.events == {"0xabc": [(22400000, 100), (22400500, -40)]}
    assert cache.block_timestamps == {22400000: 1_700_000_000, 22400500: 1_700_006_000}


def test_delegation_cache_save_load_round_trip(tmp_path):
    cache_path = tmp_path / "delegation_cache.json"
    cache = delegation.DelegationCache(
        last_synced_block=22500000,
        events={"0xabc": [(22400000, 10**24), (22400500, -(10**23))]},
        block_timestamps={22400000: 1_700_000_000},
    )

    cache.save(cache_path)

    assert delegation.DelegationCache.load(cache_path) == cache


def test_delegation_cache_load_missing_file_is_empty(tmp_path):
    assert delegation.DelegationCache.load(tmp_path / "nope.json") == delegation.DelegationCache()
