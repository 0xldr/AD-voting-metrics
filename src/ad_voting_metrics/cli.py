"""Command-line entry point with `fetch` and `finalize` subcommands.

`fetch` pulls SKY delegations from Dune and poll/spell vote data from
vote.sky.money, then writes the raw participation tabs to the workbook.
`finalize` reads the operator-reviewed Communication Master tab and
writes the Compensation tab.
"""

import argparse
import csv
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv

from . import sheets
from . import sky_dao as sky
from .compensation import compute_period_compensation
from .eligibility import DelegateMetricsInput, compute_daily_eligibility
from .period import MonthPeriod
from .reconciliation import build_entry, write_entry
from .roster import build_roster_for_period, to_dataframe
from .sources.delegates import fetch_aligned_delegates

logger = logging.getLogger(__name__)

# Locate data and output directories relative to the repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUTPUT_DIR = REPO_ROOT / "output_data"
YAML_PATH = REPO_ROOT / "delegates.yaml"
RECONCILIATION_LOG_PATH = OUTPUT_DIR / "reconciliation"


def parse_month(value):
    """Argparse type callback: parse --month value into a MonthPeriod.

    Wraps MonthPeriod.from_string() and rejects future months.

    Returns:
        The parsed MonthPeriod

    Raises:
        argparse.ArgumentTypeError: if the value is unparseable or
            resolves entirely to a future month.
    """
    try:
        period = MonthPeriod.from_string(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e

    today = date.today()
    if period.start > today:
        raise argparse.ArgumentTypeError(
            f"{value!r} resolves to {period.start.isoformat()}..{period.end.isoformat()}, "
            "which is entirely in the future."
        )

    return period


def parse_cache_hours(value: str) -> int:
    """Argparse callback for --cache-hours; requires a non-negative integer.

    Returns:
        The parsed integer.

    Raises:
        argparse.ArgumentTypeError: if the value isn't an integer or is negative.
    """
    try:
        hours = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from e
    if hours < 0:
        raise argparse.ArgumentTypeError(
            f"{value!r} is negative; --cache-hours must be 0 or greater"
        )
    return hours


def build_arg_parser():
    """Build the top-level argparse parser with `fetch` and `finalize` subcommands.

    Returns:
        The configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="ad-voting-metrics",
        description=(
            "Generate AD voting metrics for a single month. The workflow "
            "is two-step: `fetch` pulls SKY delegations and poll/spell vote "
            "data and writes the raw participation tabs to the workbook "
            "(and CSVs to output_data/)."
            "An operator then reviews communication entries manually. "
            "`finalize` reads the operator-reviewed communication data and "
            "writes the Compensation tab."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
        help="The pipeline stage to run.",
    )

    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Pull data from Dune + APIs and write participation tabs.",
        description=(
            "Step 1 of the monthly pipeline. Pulls SKY delegations from "
            "Dune Analytics and poll/spell vote data from vote.sky.money, "
            "computes participation status per (poll, delegate), and "
            "reviews communication before running 'finalize'."
        ),
    )
    fetch_parser.add_argument(
        "--month",
        required=True,
        type=parse_month,
        metavar="MONTH",
        help=(
            "Month to query, e.g. 'April 2026' or '2026-04'. Resolves to the full calendar month."
        ),
    )
    fetch_parser.add_argument(
        "--cache-hours",
        type=parse_cache_hours,
        default=None,
        metavar="N",
        help=(
            "If set, reuse a cached Dune execution if it's at most N hours old. "
            "If omitted, executes the Dune query fresh (default behavior). "
            "Use 24 or so for fast iteration during development; leave unset "
            "for production monthly run."
        ),
    )

    finalize_parser = subparsers.add_parser(
        "finalize",
        help="Compute final metrics and write Compensation tab.",
        description=(
            "Step 2 of the monthly pipeline. Reads the operator-reviewed "
            "Communication Master tab from the workbook, computes "
            "participation and communication percentages, runs Level 3 "
            "eligibility, and writes the Compensation tab. Does not "
            "re-fetch from Dune."
        ),
    )
    finalize_parser.add_argument(
        "--month",
        required=True,
        type=parse_month,
        metavar="MONTH",
        help=(
            "Month to finalize, e.g. 'April 2026' or '2026-04'. Must match - "
            "a month previously processed by `fetch`."
        ),
    )

    return parser


def check_period_has_ended(period: MonthPeriod, today: date) -> None:
    """Raise SystemExit if the period hasn't ended on or before today.

    Metrics for an in-progress period are unreliable: poll close-day rules
    can't be applied to polls still in their voting window, and the
    SKY-ranking snapshot is incomplete.

    `today` should be the current UTC date, not local - periods are
    UTC-anchored (polls close at 16:00 UTC). Pass
    `datetime.now(UTC).date()`, not `date.today()`.

    Takes `today` as a parameter so tests can pin a deterministic clock.

    Raises:
        SystemExit: if the period's last day is on or after today.
    """
    if today <= period.end:
        next_day = period.end + timedelta(days=1)
        raise SystemExit(
            f"Refusing to compute metrics for {period}: the period has not yet "
            f"ended (UTC date is {today.isoformat()}, period ends "
            f"{period.end.isoformat()}). Re-run on or after "
            f"{next_day.isoformat()} UTC."
        )
def _required_step(description: str, fn, *args, **kwargs):
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
    delegates: list, participation_by_delegate: dict, communication_by_delegate: dict
) -> dict[str, DelegateMetricsInput]:
    """Build per-delegate metrics input from workbook participation + communication data.

    Returns:
        Mapping of delegate name to DelegateMetricsInput.
    """
    metrics_input: dict[str, DelegateMetricsInput] = {}
    for delegate in delegates:
        entries = participation_by_delegate.get(delegate.name, [])
        comm_map = communication_by_delegate.get(delegate.name, {})
        poll_starts: list[date] = []
        p_statuses: list[str] = []
        c_statuses: list[str] = []
        for poll_id, poll_start, p_status in entries:
            poll_starts.append(poll_start)
            p_statuses.append(p_status)
            c_statuses.append(comm_map.get(poll_id, ""))
        metrics_input[delegate.name] = DelegateMetricsInput(
            poll_starts=poll_starts,
            participation_statuses=p_statuses,
            communication_statuses=c_statuses,
        )
    return metrics_input


def _compute_daily_results(
    period: MonthPeriod,
    window_start: date,
    window_end: date,
    delegates: list,
    daily_ranks_by_day: dict,
    metrics_input: dict[str, DelegateMetricsInput],
) -> list:
    """Compute eligibility for each day in the period.

    Returns:
        List of DailyEligibility, one per day in period.start..period.end inclusive.
    """
    daily_results = []
    current = period.start
    while current <= period.end:
        daily_results.append(
            compute_daily_eligibility(
                day=current,
                window_start=window_start,
                window_end=window_end,
                delegates=delegates,
                daily_ranks=daily_ranks_by_day.get(current, {}),
                metrics_input=metrics_input,
            )
        )
        current += timedelta(days=1)
    return daily_results


def _run_fetch(args: argparse.Namespace) -> None:
    """Pull data from Dune + APIs, write CSVs and workbook tabs."""
    period = args.month

    logger.info("Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat())

    logger.info("Building delegate roster from delegates.yaml and vote.sky.money API...")
    roster_result = build_roster_for_period(yaml_path=YAML_PATH, period=period, api_fetcher=fetch_aligned_delegates)
    delegates = roster_result.active_delegates
    drift_warnings = roster_result.drift_warnings
    for warning in drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    df = to_dataframe(delegates)

    logger.info("Getting RANKING...")
    delegate_list_sky, delegate_list_rank = sky.get_delegate_list_sky(
        df,
        period,
        cache_max_age_hours=args.cache_hours,
    )
    df_sky = pd.DataFrame(delegate_list_sky).sort_values(
        by=["date", "sky", "contract"],
        ascending=False,
    )

    df_ranking = pd.DataFrame(delegate_list_rank)
    df_ranking["Rank"] = (
        df_ranking.groupby("Date")["Total Delegation"].rank(method="first", ascending=False).astype(int)
    )
    df_ranking = df_ranking.sort_values(by=["Rank", "Date"])

    logger.info("Getting POLL IDS...")
    poll_info = sky.get_poll_ids(period)
    logger.info("Getting VOTE FROM POLLS...")
    df = sky.get_vote_poll_ids(poll_info, df, df_sky, current_datetime=datetime.now(UTC))

    logger.info("Getting SPELL addresses...")
    spell_info = sky.get_executive_ids(period)
    logger.info("Getting VOTE FROM SPELL...")
    df = sky.get_vote_executive_ids(spell_info, df, df_sky)

    output_files: list[Path] = []

    sky_csv = OUTPUT_DIR / "sky.csv"
    df_sky.to_csv(sky_csv, index=False)
    output_files.append(sky_csv)
    logger.info("SKY data by date saved to %s", sky_csv)

    participation_csv = OUTPUT_DIR / "vote_participation.csv"
    with participation_csv.open("w", newline="") as f:
        csv.writer(f).writerows(
            sheets.build_participation_values(df, poll_info, spell_info),
        )
    output_files.append(participation_csv)
    logger.info("Participation vote data saved to %s", participation_csv)

    try:
        workbook: gspread.Spreadsheet | None = sheets.get_workbook()
    except RuntimeError:
        logger.exception("Could not open Sheets workbook")
        logger.exception("CSV outputs in output_date/ are complete; skipping Sheets writes.")
        workbook = None

    if workbook is not None:

        def _safe_write(description, fn, *args):
            try:
                fn(*args)
                logger.info("%s tab written to workbook for %s", description, period)
            except ValueError as e:
                logger.error("%s writer rejected the data: %s", description, e)
            except (RuntimeError, gspread.exceptions.APIError) as e:
                logger.error("Could not write %s tab: %s", description, e)

        _safe_write(
            "Participation Raw Data", sheets.write_participation_raw_data, workbook, period, df, poll_info, spell_info
        )
        _safe_write(
            "Communication Master", sheets.write_communication_master, workbook, period, df, poll_info, spell_info
        )
        _safe_write("Daily Data", sheets.write_daily_data, workbook, period, df_ranking)

    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        yaml_config=roster_result.yaml_config,
        active_delegates=delegates,
        drift_warnings=drift_warnings,
        dune_query_id=sky.DUNE_SKY_QUERY_ID,
        dune_cache_max_age_hours=args.cache_hours,
        api_delegate_count=roster_result.api_delegate_count,
        api_fetch_succeeded=roster_result.api_fetch_succeeded,
        output_files=output_files,
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)


def _window_start_for_period(period: MonthPeriod) -> date:
    """Return the first day of the 6-month window ending in `period`.

    For April 2026 (month=4): start is November 1, 2026.
    """
    year = period.year
    month = period.month - 5
    while month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _run_finalize(args: argparse.Namespace) -> None:
    """Read workbook tabs, compute eligibility and compensation, write the Compensation tab.

    Raises:
        SystemExit: on any unrecoverable error.
    """
    period = args.month
    window_start = _window_start_for_period(period)
    window_end = period.end

    logger.info(
        "Finalize requested for %s; 6-month window %s to %s",
        period,
        window_start,
        window_end,
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
    drift_warnings = roster_result.drift_warnings
    for warning in drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    daily_ranks_by_day = _required_step("read Daily Data", sheets.read_daily_data, workbook, period)
    logger.info("Daily Data has rank rows for %d days in %s", len(daily_ranks_by_day), period)

    participation_by_delegate = sheets.read_participation_for_window(workbook, window_start, window_end)
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

    metrics_input = _build_metrics_input(delegates, participation_by_delegate, communication_by_delegate)
    daily_results = _required_step(
        "compute eligibility",
        _compute_daily_results,
        period,
        window_start,
        window_end,
        delegates,
        daily_ranks_by_day,
        metrics_input,
    )

    final_metrics: dict[str, tuple[float | None, float | None]] = {
        name: (entry.participation_pct, entry.communication_pct)
        for name, entry in daily_results[-1].per_delegate.items()
    }

    period_comp = _required_step(
        "compute compensation",
        compute_period_compensation,
        period=period,
        daily_eligibility=daily_results,
        config=config,
        final_metrics=final_metrics,
    )
    logger.info(
        "Compensation: %d delegates, slot_days_check=%s",
        len(period_comp.per_delegate),
        period_comp.validation.get("slot_days_check"),
    )

    _required_step("write Compensation tab", sheets.write_compensation_tab, workbook, period_comp)
    logger.info("Compensation tab written for %s", period)

    # Finalize doesn't hit Dune or the API and writes to a workbook tab
    # rather than a file - record the tab title in output_files so log
    # readers can tell finalize runs apart from fetch runs.
    entry = build_entry(
        period=period,
        yaml_path=YAML_PATH,
        yaml_config=roster_result.yaml_config,
        active_delegates=delegates,
        drift_warnings=drift_warnings,
        dune_query_id=sky.DUNE_SKY_QUERY_ID,
        dune_cache_max_age_hours=None,
        api_delegate_count=roster_result.api_delegate_count,
        api_fetch_succeeded=roster_result.api_fetch_succeeded,
        output_files=[Path(f"workbook:{sheets._compensation_tab_title(period)}")],
    )
    write_entry(RECONCILIATION_LOG_PATH, period, entry)


def main(argv: list[str] | None = None) -> None:
    """Entry point: configure logging, parse argv, dispatch to the chosen subcommand.

    Raises:
        SystemExit: if the period hasn't ended, on workbook or compute
            errors during `finalize`, or if argparse rejects the command line.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m%d %H-%M-%S",
    )
    logging.getLogger("dune-client").setLevel(logging.WARNING)
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    check_period_has_ended(args.month, today=datetime.now(UTC).date())

    if args.command == "fetch":
        _run_fetch(args)
    elif args.command == "finalize":
        _run_finalize(args)
    else:
        # argparse should already have rejected this; defensive fallback
        raise SystemExit(f"Unknown command: {args.command!r}")
