"""Orchestration for a single run.

Pulls SKY balances from on-chain delegation events + poll/spell vote data from vote.sky.money, writes CSV outputs to
OUTPUT_DIR, and (best-effort) writes the Participation/Communication/Daily tabs to the workbook.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import gspread
import pandas as pd
import requests
from web3.exceptions import Web3Exception

from . import sheets
from .paths import OUTPUT_DIR, RECONCILIATION_LOG_PATH, YAML_PATH
from .period import MonthPeriod
from .reconciliation import build_entry, write_entry
from .roster import build_roster_for_period, to_dataframe
from .sources import delegation, sky_executive, sky_executive_onchain, sky_polling
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)


def _build_sky_and_ranking_frames(
    df: pd.DataFrame,
    period: MonthPeriod,
    *,
    rebuild: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull on-chain SKY delegation data and build the sorted sky + ranking dataframes."""
    daily = delegation.get_delegate_list_sky(df, period, rebuild=rebuild)

    df_sky = daily[["contract", "date", "sky"]].sort_values(
        by=["date", "sky", "contract"],
        ascending=False,
    )

    df_ranking = daily[["name", "date", "sky"]].rename(
        columns={"name": "Delegate", "date": "Date", "sky": "Total Delegation"},
    )
    df_ranking["Total Delegation"] = df_ranking["Total Delegation"].round(2)
    df_ranking["Rank"] = (
        df_ranking.groupby("Date")["Total Delegation"].rank(method="first", ascending=False).astype(int)
    )
    df_ranking = df_ranking.sort_values(by=["Rank", "Date"])
    return df_sky, df_ranking


# Leading characters that spreadsheet applications interpret as a formula (or,
# for \t and \r, as field separators that can smuggle one in) when a CSV is
# opened directly in Excel/LibreOffice/Sheets.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _defuse_csv_formulas(df: pd.DataFrame) -> pd.DataFrame:
    """Prefix formula-like cells with a quote so spreadsheet apps treat them as text.

    Poll/spell titles come from external APIs, so a title like "=IMPORTDATA(...)" must not execute when the CSV is
    opened in a spreadsheet application. The apostrophe is the spreadsheet convention for "literal text"; apps hide it
    on display.

    Returns a copy of df with formula-like string cells prefixed; non-string cells unchanged.
    """

    def defuse(value: object) -> object:
        if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
            return f"'{value}"
        return value

    return df.map(defuse)


def _write_csvs(
    df: pd.DataFrame,
    df_sky: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> list[Path]:
    """Write the sky and participation CSVs.

    Participation cells are run through _defuse_csv_formulas; the CSVs are for humans and spreadsheet apps, so
    formula-like values from external APIs are neutralized at this boundary (the workbook writers neutralize
    separately via allow_formulas=False).

    Returns the list of CSV paths written, in write order.
    """
    sky_csv = OUTPUT_DIR / "sky.csv"
    df_sky.to_csv(sky_csv, index=False)
    logger.info("SKY data by date saved to %s", sky_csv)

    participation_csv = OUTPUT_DIR / "vote_participation.csv"
    participation_df = sheets.build_participation_dataframe(df, poll_info, spell_info)
    _defuse_csv_formulas(participation_df).to_csv(participation_csv, index=False)
    logger.info("Participation vote data saved to %s", participation_csv)
    return [sky_csv, participation_csv]


def _write_workbook_tabs(
    period: MonthPeriod,
    df: pd.DataFrame,
    df_ranking: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> None:
    """Open the workbook and write the participation/communication/daily tabs."""
    try:
        workbook = sheets.get_workbook()
    except RuntimeError:
        logger.exception("Could not open Sheets workbook")
        logger.info("CSV outputs in output_data/ are complete; skipping Sheets writes.")
        return

    def _safe_write(description: str, fn: Callable[..., object], *args: object) -> None:
        try:
            fn(*args)
            logger.info("%s tab written to workbook for %s", description, period)
        except ValueError:
            logger.exception("%s writer rejected the data", description)
        except RuntimeError, gspread.exceptions.APIError:
            logger.exception("Could not write %s tab", description)

    _safe_write(
        "Participation Raw Data", sheets.write_participation_raw_data, workbook, period, df, poll_info, spell_info
    )
    _safe_write("Communication Master", sheets.write_communication_master, workbook, df, poll_info, spell_info)
    _safe_write("Daily Data", sheets.write_daily_data, workbook, period, df_ranking)


def run(period: MonthPeriod, *, rebuild: bool) -> None:
    """Pull data from on-chain + APIs, write CSVs and workbook tabs."""
    logger.info("Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat())

    logger.info("Building delegate roster from delegates.yaml and vote.sky.money API...")
    roster_result = build_roster_for_period(yaml_path=YAML_PATH, period=period, api_fetcher=fetch_aligned_delegates)
    delegates = roster_result.active_delegates
    for warning in roster_result.drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    metrics = to_dataframe(delegates)

    logger.info("Getting RANKING...")
    df_sky, df_ranking = _build_sky_and_ranking_frames(metrics, period, rebuild=rebuild)
    sky_lookup = delegation.build_sky_lookup(df_sky)

    logger.info("Getting POLL IDS...")
    poll_info = sky_polling.fetch_polls_for_period(period)
    logger.info("Getting VOTE FROM POLLS...")
    metrics = sky_polling.add_poll_vote_statuses(poll_info, metrics, sky_lookup, current_datetime=datetime.now(UTC))

    logger.info("Getting SPELL addresses...")
    spell_info = sky_executive.fetch_spells_for_period(period)
    logger.info("Getting VOTE FROM SPELL...")
    metrics = sky_executive.add_spell_vote_statuses(spell_info, metrics, sky_lookup)

    logger.info("Verifying Pending executive votes on-chain...")
    try:
        metrics = sky_executive_onchain.resolve_pending_executive_votes(metrics, spell_info)
    except requests.exceptions.RequestException, Web3Exception:
        # Transient network/RPC failures are tolerable: leave cells Pending for
        # operator adjudication. Anything else (schema drift, decode/logic bugs)
        # propagates so a broken verification path fails the run instead of
        # silently no-op'ing every time.
        logger.exception("On-chain executive-vote verification failed; leaving Pending cells as-is")

    output_files = _write_csvs(metrics, df_sky, poll_info, spell_info)
    _write_workbook_tabs(period, metrics, df_ranking, poll_info, spell_info)

    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        roster=roster_result,
        delegation=delegation.read_sync_state(),
        output_files=output_files,
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)
