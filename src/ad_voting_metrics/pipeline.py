"""Orchestration for the `fetch` and `finalize` subcommands.

Imports are wired so that:
  - `fetch` pulls SKY balances from Dune + poll/spell vote data from
    vote.sky.money, writes CSV outputs to OUTPUT_DIR, and (best-effort)
    writes the Participation/Communication/Daily tabs to the workbook.
  - `finalize` reads the operator-reviewed Communication Master + the
    other workbook tabs, computes eligibility and compensation, and
    writes the Compensation tab.
"""

import argparse
import csv
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeAlias, TypeVar

import gspread
import pandas as pd

from . import sheets
from .compensation import CompensationConfig, PeriodCompensation, compute_period_compensation
from .eligibility import DailyEligibility, DelegateMetricsInput, compute_daily_eligibility
from .period import MonthPeriod
from .reconciliation import build_entry, write_entry
from .roster import Delegate, build_roster_for_period, to_dataframe
from .sources import dune, sky_executive, sky_polling
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Per-period workbook state read out of the Compensation Sheet for `finalize`:
# (daily_ranks_by_day, participation_by_delegate, communication_by_delegate).
_WorkbookData: TypeAlias = tuple[
    dict[date, dict[str, int]],
    dict[str, list[tuple[str, date, str]]],
    dict[str, dict[str, str]],
]

# Locate data and output directories relative to the repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "output_data"
YAML_PATH = REPO_ROOT / "delegates.yaml"
RECONCILIATION_LOG_PATH = OUTPUT_DIR / "reconciliation"


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
    participation_by_delegate: dict[str, list[tuple[str, date, str]]],
    communication_by_delegate: dict[str, dict[str, str]],
) -> dict[str, DelegateMetricsInput]:
    """Build per-delegate metrics input from workbook participation + communication data.

    Returns:
        Mapping of delegate name to DelegateMetricsInput.
    """
    metrics_input: dict[str, DelegateMetricsInput] = {}
    for delegate in delegates:
        entries = participation_by_delegate.get(delegate.name, [])
        comm_map = communication_by_delegate.get(delegate.name, {})
        metrics_input[delegate.name] = DelegateMetricsInput(
            poll_starts=[poll_start for _, poll_start, _ in entries],
            participation_statuses=[p_status for _, _, p_status in entries],
            communication_statuses=[comm_map.get(poll_id, "") for poll_id, _, _ in entries],
        )
    return metrics_input


def _compute_daily_results(
    period: MonthPeriod,
    window: tuple[date, date],
    *,
    delegates: list[Delegate],
    daily_ranks_by_day: dict[date, dict[str, int]],
    metrics_input: dict[str, DelegateMetricsInput],
) -> list[DailyEligibility]:
    """Compute eligibility for each day in the period.

    window is the (start, end) of the trailing metrics window, applied
    identically on each day.

    Returns:
        List of DailyEligibility, one per day in period.start..period.end inclusive.
    """
    return [
        compute_daily_eligibility(
            day=day,
            window=window,
            delegates=delegates,
            daily_ranks=daily_ranks_by_day.get(day, {}),
            metrics_input=metrics_input,
        )
        for day in pd.date_range(period.start, period.end, freq="D").date
    ]


def _build_sky_and_ranking_frames(
    df: pd.DataFrame,
    period: MonthPeriod,
    cache_hours: int | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Pull Dune SKY data and build the sorted sky + ranking dataframes.

    Returns:
        Tuple of (df_sky, df_ranking) sorted for downstream writers.
    """
    daily = dune.get_delegate_list_sky(df, period, cache_max_age_hours=cache_hours)

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


def _write_fetch_csvs(
    df: pd.DataFrame,
    df_sky: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> list[Path]:
    """Write the sky and participation CSVs.

    Returns:
        The list of CSV paths written, in write order.
    """
    sky_csv = OUTPUT_DIR / "sky.csv"
    df_sky.to_csv(sky_csv, index=False)
    logger.info("SKY data by date saved to %s", sky_csv)

    participation_csv = OUTPUT_DIR / "vote_participation.csv"
    with participation_csv.open("w", newline="") as f:
        csv.writer(f).writerows(
            sheets.build_participation_values(df, poll_info, spell_info),
        )
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
    """Pull data from Dune + APIs, write CSVs and workbook tabs."""
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
    df_sky, df_ranking = _build_sky_and_ranking_frames(df, period, args.cache_hours)

    logger.info("Getting POLL IDS...")
    poll_info = sky_polling.get_poll_ids(period)
    logger.info("Getting VOTE FROM POLLS...")
    df = sky_polling.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=datetime.now(UTC))

    logger.info("Getting SPELL addresses...")
    spell_info = sky_executive.get_executive_ids(period)
    logger.info("Getting VOTE FROM SPELL...")
    df = sky_executive.get_vote_executive_ids(spell_info, df, df_sky)

    output_files = _write_fetch_csvs(df, df_sky, poll_info, spell_info)
    _write_fetch_workbook_tabs(period, df, df_ranking, poll_info, spell_info)

    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        roster=roster_result,
        dune=(dune.DUNE_SKY_QUERY_ID, args.cache_hours),
        output_files=output_files,
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)


def _window_start_for_period(period: MonthPeriod) -> date:
    """Return the first day of the 6-month window ending in `period`.

    For April 2026 (month=4): start is November 1, 2025.
    """
    return MonthPeriod(year=period.year, month=period.month - 5).start


def _read_finalize_workbook_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    window: tuple[date, date],
) -> _WorkbookData:
    """Read Daily Data, window participation, and Communication Master from the workbook.

    Returns:
        Tuple of (daily_ranks_by_day, participation_by_delegate, communication_by_delegate).
    """
    daily_ranks_by_day = _required_step("read Daily Data", sheets.read_daily_data, workbook, period)
    logger.info("Daily Data has rank rows for %d days in %s", len(daily_ranks_by_day), period)

    participation_by_delegate = sheets.read_participation_for_window(workbook, window[0], window[1])
    logger.info(
        "Window participation: %d total (delegate, poll) entries across %d delegates",
        sum(len(v) for v in participation_by_delegate.values()),
        len(participation_by_delegate),
    )

    communication_by_delegate = _required_step(
        "read Communication Master",
        sheets.read_communication_master,
        workbook,
    )
    return daily_ranks_by_day, participation_by_delegate, communication_by_delegate


def _compute_period_compensation_for_window(
    period: MonthPeriod,
    window: tuple[date, date],
    delegates: list[Delegate],
    config: CompensationConfig,
    workbook_data: _WorkbookData,
) -> PeriodCompensation:
    """Build metrics input, compute per-day eligibility, then period compensation.

    Returns:
        The PeriodCompensation for the given period and inputs.
    """
    daily_ranks_by_day, participation_by_delegate, communication_by_delegate = workbook_data
    metrics_input = _build_metrics_input(delegates, participation_by_delegate, communication_by_delegate)
    daily_results = _required_step(
        "compute eligibility",
        _compute_daily_results,
        period,
        window,
        delegates=delegates,
        daily_ranks_by_day=daily_ranks_by_day,
        metrics_input=metrics_input,
    )
    final_metrics: dict[str, tuple[float | None, float | None]] = {
        name: (entry.participation_pct, entry.communication_pct)
        for name, entry in daily_results[-1].per_delegate.items()
    }
    return _required_step(
        "compute compensation",
        compute_period_compensation,
        period=period,
        daily_eligibility=daily_results,
        config=config,
        final_metrics=final_metrics,
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
        dune=(dune.DUNE_SKY_QUERY_ID, None),
        output_files=[Path(f"workbook:{sheets.compensation_tab_title(period)}")],
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)
