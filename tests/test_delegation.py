"""Tests for sources.delegation — on-chain event replay and daily totals."""

import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from eth_typing import HexStr
from requests.exceptions import HTTPError
from web3 import Web3
from web3.exceptions import Web3RPCError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.sources import delegation


def _ts(d: date) -> int:
    """Unix timestamp for midnight UTC on `d`."""
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def _period_stub(start: date, end: date) -> MonthPeriod:
    """Build a period-shaped stub covering [start, end].

    Tests need arbitrary date ranges; real MonthPeriod only models calendar months.
    The delegation module only reads .start/.end on the period it's handed, so a
    duck-typed SimpleNamespace stands in (cast to MonthPeriod for the type checker).
    """
    return cast("MonthPeriod", SimpleNamespace(start=start, end=end, year=start.year, month=start.month))


def test_event_topics_are_0x_prefixed_32_byte_hashes():
    """eth_getLogs rejects bare-hex topics; hexbytes>=1.0 .hex() drops the 0x prefix."""
    for topic in (delegation.LOCK_TOPIC, delegation.FREE_TOPIC):
        assert topic.startswith("0x")
        assert len(topic) == 66  # "0x" + 32 bytes * 2 hex chars


# ---------------------------------------------------------------------------
# _fetch_event_logs — merged Lock/Free fetch
# ---------------------------------------------------------------------------


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

    assert events[contract] == [[from_block + 1, "100"], [from_block + 2, "-40"]]
    assert new_blocks == {from_block + 1, from_block + 2}
    mock_w3.eth.get_logs.assert_called_once()
    params = mock_w3.eth.get_logs.call_args.args[0]
    assert params["topics"] == [[delegation.LOCK_TOPIC, delegation.FREE_TOPIC]]


# ---------------------------------------------------------------------------
# _fetch_block_timestamps — batched with sequential fallback
# ---------------------------------------------------------------------------


def test_fetch_block_timestamps_uses_one_batch_for_missing_blocks():
    mock_w3 = MagicMock()
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.return_value = [{"timestamp": 1_700_000_000}, {"timestamp": 1_700_000_100}]

    out = delegation._fetch_block_timestamps(mock_w3, {5, 6}, {})

    assert out == {"5": 1_700_000_000, "6": 1_700_000_100}
    assert batch.add.call_count == 2


def test_fetch_block_timestamps_falls_back_to_sequential_calls():
    """Providers without JSON-RPC batch support get one get_block call per block."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.side_effect = ValueError("batch not supported")
    mock_w3.eth.get_block.side_effect = [{"timestamp": 1}, {"timestamp": 2}]

    out = delegation._fetch_block_timestamps(mock_w3, {5, 6}, {})

    assert out == {"5": 1, "6": 2}
    assert mock_w3.eth.get_block.call_count == 2


def test_fetch_block_timestamps_skips_cached_blocks():
    """No RPC traffic when every block already has a cached timestamp."""
    mock_w3 = MagicMock()

    out = delegation._fetch_block_timestamps(mock_w3, {5}, {"5": 42})

    assert out == {"5": 42}
    mock_w3.batch_requests.assert_not_called()


def _http_429() -> HTTPError:
    """Build the HTTPError a provider raises when it rate-limits a request."""
    response = MagicMock()
    response.status_code = 429
    return HTTPError("429 Client Error: Too Many Requests", response=response)


def test_fetch_block_timestamps_retries_batch_after_rate_limit():
    """A rate-limited batch is retried after a backoff sleep."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [_http_429(), [{"timestamp": 7}]]

    with patch.object(delegation.time, "sleep") as mock_sleep:
        out = delegation._fetch_block_timestamps(mock_w3, {5}, {})

    assert out == {"5": 7}
    mock_sleep.assert_called_once_with(delegation.RATE_LIMIT_BASE_DELAY_SECONDS)


def test_fetch_block_timestamps_raises_after_persistent_rate_limit():
    """Rate limiting on every attempt exhausts the retry budget and surfaces the HTTPError."""
    mock_w3 = MagicMock()
    mock_w3.batch_requests.return_value.__exit__.return_value = False
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [_http_429() for _ in range(delegation.RATE_LIMIT_ATTEMPTS)]

    with patch.object(delegation.time, "sleep"), pytest.raises(HTTPError):
        delegation._fetch_block_timestamps(mock_w3, {5}, {})

    assert batch.execute.call_count == delegation.RATE_LIMIT_ATTEMPTS


def test_fetch_block_timestamps_splits_large_sets_into_multiple_batches():
    """Block sets larger than TIMESTAMP_BATCH_SIZE are fetched in multiple batches."""
    total = delegation.TIMESTAMP_BATCH_SIZE + 1
    mock_w3 = MagicMock()
    batch = mock_w3.batch_requests.return_value.__enter__.return_value
    batch.execute.side_effect = [
        [{"timestamp": i} for i in range(delegation.TIMESTAMP_BATCH_SIZE)],
        [{"timestamp": delegation.TIMESTAMP_BATCH_SIZE}],
    ]

    out = delegation._fetch_block_timestamps(mock_w3, set(range(total)), {})

    assert len(out) == total
    assert mock_w3.batch_requests.call_count == 2


# ---------------------------------------------------------------------------
# _sync_events — incremental sync against the on-disk cache
# ---------------------------------------------------------------------------


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

    w3 = MagicMock()
    w3.batch_requests.side_effect = ValueError("no batch support")
    w3.eth.get_block.side_effect = lambda n: {"timestamp": 1_700_000_000 + n}
    w3.eth.block_number = first_head
    w3.eth.get_logs.return_value = [_lock_log(contract, factory + 10, 100)]

    first = delegation._sync_events(w3, [contract], cache_path=cache_path)

    assert w3.eth.get_logs.call_args.args[0]["fromBlock"] == factory
    assert first["last_synced_block"] == factory + 1_000
    assert cache_path.exists()

    w3.eth.block_number = first_head + 500
    w3.eth.get_logs.return_value = [_lock_log(contract, first_head + 100, 50)]

    second = delegation._sync_events(w3, [contract], cache_path=cache_path)

    assert w3.eth.get_logs.call_args.args[0]["fromBlock"] == factory + 1_001
    assert second["events"][contract] == [[factory + 10, "100"], [first_head + 100, "50"]]
    assert second["last_synced_block"] == factory + 1_500
    assert delegation._load_cache(cache_path)["events"][contract] == second["events"][contract]


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

    out = delegation._sync_events(w3, [contract], cache_path=cache_path)

    w3.eth.get_logs.assert_not_called()
    assert out["last_synced_block"] == synced_to
    assert cache_path.stat().st_mtime_ns == mtime


# ---------------------------------------------------------------------------
# get_all_sky_delegated — event sync, cache load/save, daily series
# ---------------------------------------------------------------------------


def test_get_all_sky_delegated_raises_if_rpc_url_missing(monkeypatch):
    """If SKY_RPC_URL is unset and w3 not injected, raise RuntimeError."""
    monkeypatch.delenv("SKY_RPC_URL", raising=False)

    with pytest.raises(RuntimeError, match="SKY_RPC_URL"):
        delegation.get_all_sky_delegated(["0x" + "a" * 40], w3=None)


def test_get_all_sky_delegated_uses_injected_w3(tmp_path):
    """If w3 is provided, ignore SKY_RPC_URL."""
    cache_path = tmp_path / "delegation_cache.json"
    contract = "0x" + "c" * 40

    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 22368738
    mock_w3.eth.get_logs.return_value = []  # No events

    # Should not raise even though SKY_RPC_URL unset.
    result = delegation.get_all_sky_delegated(
        [contract],
        w3=mock_w3,
        cache_path=cache_path,
    )

    assert isinstance(result, pd.DataFrame)


def test_get_all_sky_delegated_rebuild_resyncs_from_factory_block(monkeypatch, tmp_path):
    """rebuild=True discards cache and resyncs from V3_FACTORY_BLOCK."""
    monkeypatch.setenv("SKY_RPC_URL", "http://localhost:8545")
    cache_path = tmp_path / "delegation_cache.json"

    # Pre-populate cache with stale last_synced_block.
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    cache_path.write_text(json.dumps({"last_synced_block": 99999999}))

    mock_w3 = MagicMock()
    mock_w3.eth.block_number = 100000000
    mock_w3.eth.get_logs.return_value = []  # No events

    with patch("ad_voting_metrics.sources.delegation._sync_events") as sync_mock:
        sync_mock.return_value = {"events": {}, "last_synced_block": 100000000}
        delegation.get_all_sky_delegated(
            ["0xaaaa"],
            w3=mock_w3,
            cache_path=cache_path,
            rebuild=True,
        )

        sync_mock.assert_called_once()
        assert sync_mock.call_args[1]["rebuild"] is True


def test_get_all_sky_delegated_raises_when_getlogs_always_fails(tmp_path):
    """A provider that rejects every getLogs range surfaces a RuntimeError, not an infinite loop."""
    cache_path = tmp_path / "delegation_cache.json"
    mock_w3 = MagicMock()
    mock_w3.eth.block_number = delegation.V3_FACTORY_BLOCK + 100_000
    mock_w3.eth.get_logs.side_effect = Web3RPCError("range too large")

    with pytest.raises(RuntimeError, match="minimum chunk size"):
        delegation.get_all_sky_delegated(["0x" + "a" * 40], w3=mock_w3, cache_path=cache_path, rebuild=True)


def test_get_all_sky_delegated_recovers_by_shrinking_chunk(tmp_path):
    """getLogs ranges that are too large are retried at smaller sizes until one succeeds."""
    cache_path = tmp_path / "delegation_cache.json"

    def get_logs(params: dict) -> list:
        if params["toBlock"] - params["fromBlock"] + 1 > delegation.MIN_CHUNK_BLOCKS:
            raise Web3RPCError("range too large")
        return []

    mock_w3 = MagicMock()
    mock_w3.eth.block_number = delegation.V3_FACTORY_BLOCK + 5_000
    mock_w3.eth.get_logs.side_effect = get_logs

    result = delegation.get_all_sky_delegated(["0x" + "a" * 40], w3=mock_w3, cache_path=cache_path, rebuild=True)
    assert isinstance(result, pd.DataFrame)
    assert result.empty


# ---------------------------------------------------------------------------
# _contract_cumulative_balances / _build_balance_series — running-total arithmetic
# ---------------------------------------------------------------------------


def test_contract_cumulative_balances_nets_same_day_events_and_carries_total():
    """Two events on one day collapse to one entry; later days start from the prior running total."""
    day_1, day_3 = date(2026, 4, 1), date(2026, 4, 3)
    timestamps = {"1": _ts(day_1), "2": _ts(day_1) + 3600, "3": _ts(day_3)}
    events = [[1, "100"], [2, "-40"], [3, "10"]]

    out = delegation._contract_cumulative_balances(events, timestamps, "0xc")

    assert out == {day_1: 60, day_3: 70}


def test_contract_cumulative_balances_raises_when_total_goes_negative():
    timestamps = {"1": _ts(date(2026, 4, 1))}

    with pytest.raises(ValueError, match="Negative running total"):
        delegation._contract_cumulative_balances([[1, "-5"]], timestamps, "0xc")


def test_contract_cumulative_balances_skips_events_without_cached_timestamp(caplog):
    day = date(2026, 4, 1)
    timestamps = {"1": _ts(day)}

    with caplog.at_level("WARNING"):
        out = delegation._contract_cumulative_balances([[1, "100"], [2, "100"]], timestamps, "0xc")

    assert out == {day: 100}
    assert "no cached timestamp" in caplog.text


def test_build_balance_series_converts_wei_to_sky_one_row_per_event_day():
    day_1, day_5 = date(2026, 4, 1), date(2026, 4, 5)
    cache = {
        "events": {"0xc": [[1, str(2 * 10**18)], [2, str(10**18)]]},
        "block_timestamps": {"1": _ts(day_1), "2": _ts(day_5)},
    }

    out = delegation._build_balance_series(cache)

    assert out.index.names == ["delegation_contract", "dt"]
    assert out["running_total_balance"].to_dict() == {("0xc", day_1): 2.0, ("0xc", day_5): 3.0}


def test_build_balance_series_empty_cache_returns_empty_indexed_frame():
    out = delegation._build_balance_series({})

    assert out.empty
    assert out.index.names == ["delegation_contract", "dt"]
    assert list(out.columns) == ["running_total_balance"]


# ---------------------------------------------------------------------------
# get_delegate_list_sky — per-day rows + zero-fill
# ---------------------------------------------------------------------------


def _sky_df_indexed(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    """Return a DataFrame shaped like get_all_sky_delegated's return value.

    rows: (contract, dt_iso, running_total_balance) tuples.
    """
    df = pd.DataFrame(rows, columns=["delegation_contract", "dt", "running_total_balance"])
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    return df.set_index(["delegation_contract", "dt"])


def test_get_delegate_list_sky_returns_one_row_per_delegate_per_day():
    """For a 2-day period with 1 delegate, output covers both days."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": date(2024, 1, 1)},
        ]
    )
    fake_all_sky = _sky_df_indexed(
        [
            ("0xaaa", "2026-04-01", 1000.0),
            ("0xaaa", "2026-04-02", 1500.0),
        ]
    )
    period_2day = _period_stub(date(2026, 4, 1), date(2026, 4, 2))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_2day)

    assert len(result) == 2
    assert list(result.columns) == ["contract", "name", "date", "sky"]


def test_get_delegate_list_sky_carries_balance_forward_and_zeros_before_first_event():
    """Balance set on day 2 is zero on day 1 (before first event) and carried forward to day 3."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": date(2024, 1, 1)},
        ]
    )
    fake_all_sky = _sky_df_indexed(
        [
            ("0xaaa", "2026-04-02", 1500.0),
            # day 1 precedes the first event; day 3 has no new event.
        ]
    )
    period_3day = _period_stub(date(2026, 4, 1), date(2026, 4, 3))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_3day)

    by_date = dict(zip(result["date"], result["sky"], strict=True))
    assert by_date[date(2026, 4, 1)] == 0.0  # before first event: no balance yet
    assert by_date[date(2026, 4, 2)] == 1500.0
    assert by_date[date(2026, 4, 3)] == 1500.0  # carried forward from the last known balance


def test_get_delegate_list_sky_carries_pre_period_balance_across_whole_period():
    """A delegate whose last Lock/Free predates the period keeps that balance every in-period day."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": date(2024, 1, 1)},
        ]
    )
    # Last (and only) event is in March; the queried period is April, with no April events.
    fake_all_sky = _sky_df_indexed([("0xaaa", "2026-03-15", 1_000_000.0)])
    period_april = _period_stub(date(2026, 4, 1), date(2026, 4, 3))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_april)

    assert list(result["sky"]) == [1_000_000.0, 1_000_000.0, 1_000_000.0]


def test_get_delegate_list_sky_lowercases_name():
    """The name column is the delegate's name lowercased and stripped."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "  Alice  ", "Delegate Contract": "0xaaa", "Start Date": date(2024, 1, 1)},
        ]
    )
    fake_all_sky = _sky_df_indexed([("0xaaa", "2026-04-01", 1000.0)])
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_1day)

    assert result.iloc[0]["name"] == "alice"


def test_get_delegate_list_sky_multiple_delegates():
    """Two delegates produce separate (contract, name, sky) entries per day."""
    df = pd.DataFrame(
        [
            {"Delegate Name": "Alice", "Delegate Contract": "0xaaa", "Start Date": date(2024, 1, 1)},
            {"Delegate Name": "Bob", "Delegate Contract": "0xbbb", "Start Date": date(2024, 1, 1)},
        ]
    )
    fake_all_sky = _sky_df_indexed(
        [
            ("0xaaa", "2026-04-01", 1000.0),
            ("0xbbb", "2026-04-01", 500.0),
        ]
    )
    period_1day = _period_stub(date(2026, 4, 1), date(2026, 4, 1))

    with patch.object(delegation, "get_all_sky_delegated", return_value=fake_all_sky):
        result = delegation.get_delegate_list_sky(df, period_1day)

    by_name = dict(zip(result["name"], result["sky"], strict=True))
    assert by_name == {"alice": 1000.0, "bob": 500.0}


# ---------------------------------------------------------------------------
# build_sky_lookup — O(1) dict from DataFrame
# ---------------------------------------------------------------------------


def test_build_sky_lookup_returns_dict_keyed_by_contract_date():
    """Materialize df_sky into (contract, date) -> balance dict."""
    df_sky = pd.DataFrame(
        [
            {"contract": "0xaaa", "date": date(2026, 4, 1), "sky": 1000.0},
            {"contract": "0xbbb", "date": date(2026, 4, 1), "sky": 500.0},
        ]
    )

    result = delegation.build_sky_lookup(df_sky)

    assert result["0xaaa", date(2026, 4, 1)] == 1000.0
    assert result["0xbbb", date(2026, 4, 1)] == 500.0


# ---------------------------------------------------------------------------
# read_sync_state — cache metadata
# ---------------------------------------------------------------------------


def test_read_sync_state_returns_factory_block_and_last_synced(tmp_path):
    """read_sync_state fetches factory_block and last_synced_block from cache."""
    cache_path = tmp_path / "delegation_cache.json"

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps({"last_synced_block": 22500000}))

    result = delegation.read_sync_state(cache_path)

    assert result["factory_block"] == 22368737
    assert result["last_synced_block"] == 22500000


def test_read_sync_state_returns_factory_block_if_cache_empty(tmp_path):
    """If cache doesn't exist or is empty, last_synced_block defaults to factory_block."""
    cache_path = tmp_path / "delegation_cache.json"

    result = delegation.read_sync_state(cache_path)

    assert result["factory_block"] == 22368737
    assert result["last_synced_block"] == 22368737
