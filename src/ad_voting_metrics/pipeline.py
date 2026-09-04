"""Orchestration for a single run.

Builds the roster, pulls SKY balances from on-chain delegation events and poll/spell vote data from vote.sky.money,
adjudicates pending executive votes on-chain, then writes the month's CSVs and a reconciliation log entry.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import requests
from web3 import Web3
from web3.exceptions import Web3Exception

from .outputs import write_csvs
from .period import MonthPeriod
from .reconciliation import build_entry, write_entry
from .roster import build_roster_for_period
from .sources import delegation, sky_executive, sky_executive_onchain, sky_polling
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)


def _rank_daily_balances(daily: pd.DataFrame) -> pd.DataFrame:
    """Add a per-day rank (1 = most SKY delegated) and sort by date, then rank.

    Ties break by row order, so two delegates with identical balances never share a rank.

    Returns:
        The (contract, name, date, sky, rank) frame.
    """
    rank = daily.groupby("date")["sky"].rank(method="first", ascending=False).astype(int)
    return daily.assign(rank=rank).sort_values(["date", "rank"]).reset_index(drop=True)


def run(period: MonthPeriod, *, rebuild: bool, roster_path: Path, output_dir: Path, w3: Web3) -> None:
    """Pull data from on-chain + APIs and write the month's CSVs under output_dir/<YYYY-MM>/.

    output_dir also holds the on-chain event caches and the reconciliation log. `w3` is used for both the delegation
    sync and the executive-vote verification.
    """
    delegation_cache = output_dir / "delegation_cache.json"
    slate_cache = output_dir / "slate_cache.json"
    month_dir = output_dir / period.start.strftime("%Y-%m")

    logger.info("Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat())

    logger.info("Building delegate roster from %s and vote.sky.money API...", roster_path)
    roster_result = build_roster_for_period(yaml_path=roster_path, period=period, api_fetcher=fetch_aligned_delegates)
    delegates = roster_result.active_delegates
    for warning in roster_result.drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    logger.info("Fetching daily SKY balances...")
    daily = _rank_daily_balances(
        delegation.get_delegate_list_sky(delegates, period, w3=w3, cache_path=delegation_cache, rebuild=rebuild)
    )
    sky_lookup = delegation.build_sky_lookup(daily)

    logger.info("Fetching polls...")
    polls = sky_polling.fetch_polls_for_period(period)
    logger.info("Fetching poll votes...")
    statuses = sky_polling.poll_statuses(polls, delegates, sky_lookup, current_datetime=datetime.now(UTC))

    logger.info("Fetching executive spells...")
    spells = sky_executive.fetch_spells_for_period(period)
    logger.info("Seeding spell statuses...")
    statuses |= sky_executive.spell_statuses(spells, delegates, sky_lookup)

    logger.info("Verifying Pending executive votes on-chain...")
    try:
        statuses = sky_executive_onchain.resolve_pending_executive_votes(
            statuses, spells, w3=w3, cache_path=slate_cache
        )
    except requests.exceptions.RequestException, Web3Exception:
        # Transient network/RPC failures are tolerable: leave cells Pending for
        # operator adjudication. Anything else (schema drift, decode/logic bugs)
        # propagates so a broken verification path fails the run instead of
        # silently no-op'ing every time.
        logger.exception("On-chain executive-vote verification failed; leaving Pending cells as-is")

    output_files = write_csvs(month_dir, daily, delegates, [*polls, *spells], statuses)

    entry = build_entry(
        period=period,
        yaml_path=roster_path,
        roster=roster_result,
        delegation=delegation.read_sync_state(delegation_cache),
        output_files=output_files,
    )
    write_entry(output_dir / "reconciliation", period, entry)
