"""Orchestration for the `fetch` and `finalize` subcommands.

Imports are wired so that:
  - `fetch` pulls SKY balances from on-chain delegation events + poll/spell vote
    data from vote.sky.money, writes CSV outputs to OUTPUT_DIR,
    and (best-effort) writes the Participation/Communication/Daily tabs to the workbook.
  - `finalize` reads the operator-reviewed Communication Master + the other workbook tabs,
    computes eligibility and compensation, and writes the Compensation tab.
"""

import argparse
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeAlias, TypeVar

import gspread
import pandas as pd
import requests
from web3.exceptions import Web3Exception

from . import sheets
from .compensation import CompensationConfig, PeriodCompensation, compute_period_compensation
from .eligibility import DailyEligibility, DelegateMetricsInput, compute_daily_eligibility, compute_period_metrics
from .paths import OUTPUT_DIR, RECONCILIATION_LOG_PATH, YAML_PATH
from .period import MonthPeriod
from .reconciliation import build_entry, write_entry
from .roster import Delegate, build_roster_for_period, to_dataframe
from .sources import delegation, sky_executive, sky_executive_onchain, sky_polling
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Per-period workbook state read out of the Compensation Sheet for `finalize`.
# All three DataFrames are read directly from the workbook by sheets.read_*.
_WorkbookData: TypeAlias = tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]


def _required_step(description: str, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
    """Run an IO/compute step; on failure, log, and exit.

    Returns:
        Whatever fn(*args, **kwargs) returns.

    Raises:
        SystemExit: on any caught exception.
    """
    try:
        return fn(*args, **kwargs)
    except (RuntimeError, ValueError, gspread.exceptions.APIError) as e:
        logger.exception("Could not %s", description)
        raise SystemExit(1) from e


def _build_metrics_input(
    delegates: list[Delegate],
    participation_df: pd.DataFrame,
    communication_df: pd.DataFrame,
) -> dict[str, DelegateMetricsInput]:
    """Build per-delegate metrics input from window participation + communication DataFrames.

    `participation_df` has one row per (delegate, poll) with Start Date and Participation Status; `communication_df` has
    one row per (delegate, poll) with Communication Status. They are merged on (Delegate, Poll Id); polls absent from
    communication get a blank communication status.


    """
    if participation_df.empty:
        return {
            delegate.name: DelegateMetricsInput(
                poll_starts=[],
                participation_statuses=[],
                communication_statuses=[],
            )
            for delegate in delegates
        }

    merged = participation_df.merge(
        communication_df[["Delegate", "Poll Id", "Communication Status"]]
        if not communication_df.empty
        else pd.DataFrame(columns=["Delegate", "Poll Id", "Communication Status"]),
        on=["Delegate", "Poll Id"],
        how="left",
    )
    merged["Communication Status"] = merged["Communication Status"].fillna("")

    rows_by_delegate = dict(tuple(merged.groupby("Delegate")))
    empty = merged.iloc[0:0]
    metrics_input: dict[str, DelegateMetricsInput] = {}
    for delegate in delegates:
        rows = rows_by_delegate.get(delegate.name, empty)
        metrics_input[delegate.name] = DelegateMetricsInput(
            poll_starts=rows["Start Date"].tolist(),
            participation_statuses=rows["Participation Status"].tolist(),
            communication_statuses=rows["Communication Status"].tolist(),
        )
    return metrics_input


def _ranks_by_day(daily_ranks_df: pd.DataFrame) -> dict[date, dict[str, int]]:
    """Pivot the long Daily Data DataFrame into {day: {delegate: rank}}."""
    if daily_ranks_df.empty:
        return {}
    out: dict[date, dict[str, int]] = {}
    for day_key, group in daily_ranks_df.groupby("Date"):
        # groupby's key is typed as Hashable; the column actually holds date objects.
        day = day_key if isinstance(day_key, date) else date.fromisoformat(str(day_key))
        out[day] = dict(zip(group["Delegate"], group["Rank"].astype(int), strict=False))
    return out


def _compute_daily_results(
    period: MonthPeriod,
    *,
    delegates: list[Delegate],
    daily_ranks_df: pd.DataFrame,
    period_metrics: dict[str, tuple[float | None, float | None]],
    total_slots: int,
) -> list[DailyEligibility]:
    """Compute eligibility for each day in the period.

    The 6-month metric window is constant across the period, so the caller computes (p_pct, c_pct) per delegate once
    via `compute_period_metrics` and passes it in for reuse across every day. The only per-day inputs are the rank
    table and the day's L3 slot competition. `total_slots` comes from the workbook Config tab.

    Returns a list of DailyEligibility, one per day in period.start..period.end inclusive.

    """
    ranks_by_day = _ranks_by_day(daily_ranks_df)
    return [
        compute_daily_eligibility(
            day=day,
            delegates=delegates,
            daily_ranks=ranks_by_day.get(day, {}),
            period_metrics=period_metrics,
            total_slots=total_slots,
        )
        for day in pd.date_range(period.start, period.end, freq="D").date
    ]


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


def _write_fetch_csvs(
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


def _write_fetch_workbook_tabs(
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
        except (RuntimeError, gspread.exceptions.APIError):
            logger.exception("Could not write %s tab", description)

    _safe_write(
        "Participation Raw Data", sheets.write_participation_raw_data, workbook, period, df, poll_info, spell_info
    )
    _safe_write("Communication Master", sheets.write_communication_master, workbook, df, poll_info, spell_info)
    _safe_write("Daily Data", sheets.write_daily_data, workbook, period, df_ranking)


def run_fetch(args: argparse.Namespace) -> None:
    """Pull data from on-chain + APIs, write CSVs and workbook tabs."""
    period = args.month

    logger.info("Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat())

    logger.info("Building delegate roster from delegates.yaml and vote.sky.money API...")
    roster_result = build_roster_for_period(yaml_path=YAML_PATH, period=period, api_fetcher=fetch_aligned_delegates)
    delegates = roster_result.active_delegates
    for warning in roster_result.drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    df = to_dataframe(delegates)

    logger.info("Getting RANKING...")
    df_sky, df_ranking = _build_sky_and_ranking_frames(df, period, rebuild=args.rebuild)
    sky_lookup = delegation.build_sky_lookup(df_sky)

    logger.info("Getting POLL IDS...")
    poll_info = sky_polling.fetch_polls_for_period(period)
    logger.info("Getting VOTE FROM POLLS...")
    df = sky_polling.add_poll_vote_statuses(poll_info, df, sky_lookup, current_datetime=datetime.now(UTC))

    logger.info("Getting SPELL addresses...")
    spell_info = sky_executive.fetch_spells_for_period(period)
    logger.info("Getting VOTE FROM SPELL...")
    df = sky_executive.add_spell_vote_statuses(spell_info, df, sky_lookup)

    logger.info("Verifying Pending executive votes on-chain...")
    try:
        df = sky_executive_onchain.resolve_pending_executive_votes(df, spell_info)
    except (requests.exceptions.RequestException, Web3Exception):
        # Transient network/RPC failures are tolerable: leave cells Pending for
        # operator adjudication. Anything else (schema drift, decode/logic bugs)
        # propagates so a broken verification path fails the run instead of
        # silently no-op'ing every time.
        logger.exception("On-chain executive-vote verification failed; leaving Pending cells as-is")

    output_files = _write_fetch_csvs(df, df_sky, poll_info, spell_info)
    _write_fetch_workbook_tabs(period, df, df_ranking, poll_info, spell_info)

    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        roster=roster_result,
        delegation=delegation.read_sync_state(),
        output_files=output_files,
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)


def _window_start_for_period(period: MonthPeriod) -> date:
    """Return the first day of the 6-month window ending in `period`.

    For April 2026 (month=4): start is November 1, 2025.
    """
    return period.minus_months(5).start


def _read_finalize_workbook_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    window: tuple[date, date],
) -> _WorkbookData:
    """Read Daily Data, window participation, and Communication Master from the workbook.

    Returns:
        Tuple of (daily_df, participation_df, communication_df).
    """
    daily_df = _required_step("read Daily Data", sheets.read_daily_data, workbook, period)
    logger.info("Daily Data has rank rows for %d days in %s", daily_df["Date"].nunique(), period)

    participation_df = _required_step(
        "read window participation",
        sheets.read_participation_for_window,
        workbook,
        window[0],
        window[1],
    )
    logger.info(
        "Window participation: %d (delegate, poll) entries across %d delegates",
        len(participation_df),
        participation_df["Delegate"].nunique() if not participation_df.empty else 0,
    )

    communication_df = _required_step(
        "read Communication Master",
        sheets.read_communication_master,
        workbook,
    )
    return daily_df, participation_df, communication_df


def _compute_period_compensation_for_window(
    period: MonthPeriod,
    window: tuple[date, date],
    delegates: list[Delegate],
    config: CompensationConfig,
    workbook_data: _WorkbookData,
) -> PeriodCompensation:
    """Build metrics input, compute per-day eligibility, then period compensation."""
    daily_df, participation_df, communication_df = workbook_data
    metrics_input = _build_metrics_input(delegates, participation_df, communication_df)
    # Period-level metrics cover every delegate active at any point (the last day's slice omits
    # mid-period exiters). Computed once and shared by daily eligibility and compensation.
    period_metrics = _required_step(
        "compute period metrics",
        compute_period_metrics,
        window,
        delegates,
        metrics_input,
    )
    daily_results = _required_step(
        "compute eligibility",
        _compute_daily_results,
        period,
        delegates=delegates,
        daily_ranks_df=daily_df,
        period_metrics=period_metrics,
        total_slots=config.total_slots,
    )
    return _required_step(
        "compute compensation",
        compute_period_compensation,
        period=period,
        daily_eligibility=daily_results,
        config=config,
        final_metrics=period_metrics,
    )


def run_finalize(args: argparse.Namespace) -> None:
    """Read workbook tabs, compute eligibility and compensation, write the Compensation tab."""
    period = args.month
    window = (_window_start_for_period(period), period.end)

    logger.info(
        "Finalize requested for %s; 6-month window %s to %s",
        period,
        window[0],
        window[1],
    )

    workbook = _required_step("open Sheets workbook", sheets.get_workbook)
    config = _required_step("read Config tab", sheets.read_config, workbook)
    logger.info(
        "Config: L1=%s L2=%s L3=%s total_slots=%d",
        config.l1_usds,
        config.l2_usds,
        config.l3_usds,
        config.total_slots,
    )

    logger.info("Building delegate roster from delegates.yaml...")
    roster_result = build_roster_for_period(
        yaml_path=YAML_PATH,
        period=period,
        api_fetcher=fetch_aligned_delegates,
        skip_api_check=True,
    )
    delegates = roster_result.active_delegates
    for warning in roster_result.drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    workbook_data = _read_finalize_workbook_data(workbook, period, window)

    period_comp = _compute_period_compensation_for_window(period, window, delegates, config, workbook_data)
    logger.info(
        "Compensation: %d delegates, slot_days_check=%s",
        len(period_comp.per_delegate),
        period_comp.validation.get("slot_days_check"),
    )

    _required_step("write Compensation tab", sheets.write_compensation_tab, workbook, period_comp)
    logger.info("Compensation tab written for %s", period)

    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        roster=roster_result,
        delegation=None,
        output_files=[Path(f"workbook:{sheets.compensation_tab_title(period)}")],
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)
