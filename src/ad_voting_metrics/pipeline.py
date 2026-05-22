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
from .sources import dune, sky_executive, sky_executive_onchain, sky_polling
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Per-period workbook state read out of the Compensation Sheet for `finalize`.
# All three DataFrames are read directly from the workbook by sheets.read_*.
_WorkbookData: TypeAlias = tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]

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
    participation_df: pd.DataFrame,
    communication_df: pd.DataFrame,
) -> dict[str, DelegateMetricsInput]:
    """Build per-delegate metrics input from window participation + communication DataFrames.

    `participation_df` has one row per (delegate, poll) with Start Date
    and Participation Status; `communication_df` has one row per
    (delegate, poll) with Communication Status. They are merged on
    (Delegate, Poll Id); polls absent from communication get a blank
    communication status.

    Returns:
        Mapping of delegate name to DelegateMetricsInput.
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

    metrics_input: dict[str, DelegateMetricsInput] = {}
    for delegate in delegates:
        rows = merged[merged["Delegate"] == delegate.name]
        metrics_input[delegate.name] = DelegateMetricsInput(
            poll_starts=rows["Start Date"].tolist(),
            participation_statuses=rows["Participation Status"].tolist(),
            communication_statuses=rows["Communication Status"].tolist(),
        )
    return metrics_input


def _ranks_by_day(daily_ranks_df: pd.DataFrame) -> dict[date, dict[str, int]]:
    """Pivot the long Daily Data DataFrame into {day: {delegate: rank}}.

    Returns:
        Mapping from each day present in the DataFrame to its
        {delegate: rank} for the day.
    """
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
    window: tuple[date, date],
    *,
    delegates: list[Delegate],
    daily_ranks_df: pd.DataFrame,
    metrics_input: dict[str, DelegateMetricsInput],
) -> list[DailyEligibility]:
    """Compute eligibility for each day in the period.

    window is the (start, end) of the trailing metrics window, applied
    identically on each day.

    Returns:
        List of DailyEligibility, one per day in period.start..period.end inclusive.
    """
    ranks_by_day = _ranks_by_day(daily_ranks_df)
    return [
        compute_daily_eligibility(
            day=day,
            window=window,
            delegates=delegates,
            daily_ranks=ranks_by_day.get(day, {}),
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
    participation_df = sheets.build_participation_dataframe(df, poll_info, spell_info)
    participation_df.to_csv(participation_csv, index=False)
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

    logger.info("Verifying Pending executive votes on-chain...")
    try:
        df = sky_executive_onchain.resolve_pending_executive_votes(df, spell_info)
    except Exception:
        logger.exception("On-chain executive-vote verification failed; leaving Pending cells as-is")

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
        Tuple of (daily_df, participation_df, communication_df).
    """
    daily_df = _required_step("read Daily Data", sheets.read_daily_data, workbook, period)
    logger.info("Daily Data has rank rows for %d days in %s", daily_df["Date"].nunique(), period)

    participation_df = sheets.read_participation_for_window(workbook, window[0], window[1])
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
    """Build metrics input, compute per-day eligibility, then period compensation.

    Returns:
        The PeriodCompensation for the given period and inputs.
    """
    daily_df, participation_df, communication_df = workbook_data
    metrics_input = _build_metrics_input(delegates, participation_df, communication_df)
    daily_results = _required_step(
        "compute eligibility",
        _compute_daily_results,
        period,
        window,
        delegates=delegates,
        daily_ranks_df=daily_df,
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
