import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import gspread
import pandas as pd
from dotenv import load_dotenv

from . import sheets
from . import sky_dao as sky
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
    """argparse type callback: parse --month value into a MonthPeriod.

    Wraps MonthPeriod.from_string() and adds CLI-specific concerns:
      - Errors are surfaced as argparse.ArgumentTypeErrors so argparse handles
      them with proper messages and exit code 2.
      - Future months are rejected here.
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
    """argparse callback for --cache-hours.

    Accepts a non-negative integer. Negative values are rejected.
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
    parser = argparse.ArgumentParser(
        prog="ad-voting-metrics",
        description=(
            "Generate AD voting metrics for a single month. The workflow "
            "is two-step: `fetch` pulls SKY delegations and poll/spell vote "
            "data and writes the raw participation tabs to the workbook "
            "(and CSVs to output_data/ for the current transition period). "
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
    can't be applied to polls still in their voting window, the SKY-ranking
    snapshot is incomplete, and the operator workflow assumes a closed period
    boundary.

    `today` should be the current UTC date - periods are UTC-anchored
    (polls close at 16:00 UTC).
    """
    if today <= period.end:
        next_day = period.end + timedelta(days=1)
        raise SystemExit(
            f"Refusing to compute metrics for {period}: the period has not yet "
            f"ended (UTC date is {today.isoformat()}, period ends "
            f"{period.end.isoformat()}). Re-run on or after "
            f"{next_day.isoformat()} UTC."
        )


def _run_fetch(args: argparse.Namespace) -> None:
    """Execute the fetch stage: pull from Dune + APIs, write raw participation."""
    period = args.month

    logger.info(
        "Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat()
    )

    # Step 1: Build the delegate roster from delegates.yaml + vote.sky.money API.
    # The YAML is the source of truth, the API call is for drift detection.
    # Filtering to "active during this month" happens inside build_roster_for_period.
    logger.info("Building delegate roster from delegates.yaml and vote.sky.money API...")
    roster_result = build_roster_for_period(
        yaml_path=YAML_PATH,
        period=period,
        api_fetcher=fetch_aligned_delegates,
    )
    delegates = roster_result.active_delegates
    drift_warnings = roster_result.drift_warnings
    for warning in drift_warnings:
        logger.warning(warning)
    logger.info("Roster has %d delegates active during %s", len(delegates), period)

    # Track CSVs we write so the reconciliation log can record them
    output_files: list[Path] = []

    df = to_dataframe(delegates)
    hardcoded_order = df["Delegate Contract"].str.lower().tolist()

    # Get delegate list and SKY ranking
    logger.info("Getting RANKING...")
    delegate_list_sky, delegate_list_rank = sky.get_delegate_list_sky(
        df,
        period,
        cache_max_age_hours=args.cache_hours,
    )

    df_sky = pd.DataFrame(delegate_list_sky)
    df_ranking = pd.DataFrame(delegate_list_rank)

    df_sky = df_sky.sort_values(by=["date", "sky", "contract"], ascending=False)
    df_ranking = df_ranking.sort_values(by=["Date", "Total Delegation"], ascending=False)

    # Calculate and assign ranks to delegates
    current_rank = 1
    prev_date = None
    ranks = []

    for _index, row in df_ranking.iterrows():
        if row["Date"] != prev_date:
            current_rank = 1
        ranks.append(current_rank)
        current_rank += 1
        prev_date = row["Date"]

    df_ranking["Rank"] = ranks
    df_ranking = df_ranking.sort_values(by=["Rank", "Date"], ascending=[True, True])

    # Get poll IDs information and vote from polls
    logger.info("Getting POLL IDS...")
    POLL_INFO = sky.get_poll_ids(period)
    logger.info("Getting VOTE FROM POLLS...")
    df = sky.get_vote_poll_ids(POLL_INFO, df, df_sky, current_datetime=datetime.now(UTC))

    # Get SPELL addresses information and vote from SPELL
    logger.info("Getting SPELL addresses...")
    SPELL_INFO = sky.get_execute_ids(period)
    logger.info("Getting VOTE FROM SPELL...")
    df = sky.get_vote_execute_ids(SPELL_INFO, df, df_sky)

    # Save data to CSV files
    output_csv = OUTPUT_DIR / "vote_participation.csv"
    df.to_csv(output_csv, index=False)
    output_files.append(output_csv)
    logger.info("Participation vote data saved to %s", output_csv)

    # Open the workbook once and reuse for all writers below. Failures are
    # handled per-writer so one tab's problem doesn't drop the others. The
    # CSV outputs above are always written before this block, so they
    # remain available as a fallback during the transition phase.
    workbook: gspread.Spreadsheet | None
    try:
        workbook = sheets.get_workbook()
    except RuntimeError as e:
        logger.error("Could not open Sheets workbook: %s", e)
        logger.error("CSV outputs in output_date/ are complete; skipping Sheets writes.")
        workbook = None

    # Write Participation Raw Data BEFORE custom_sort, which transposes df.
    # The writer expects the per-delegate-rows shape.
    if workbook is not None:
        try:
            sheets.write_participation_raw_data(
                workbook,
                period,
                df,
                POLL_INFO,
                SPELL_INFO,
            )
            logger.info("Participation Raw Data tab written to workbook for %s", period)
        except (RuntimeError, gspread.exceptions.APIError) as e:
            logger.error("Could not write Participation Raw Data tab: %s", e)

    df = sky.custom_sort(df, hardcoded_order, POLL_INFO, SPELL_INFO)

    output_csv = OUTPUT_DIR / "sky.csv"
    df_sky.to_csv(output_csv, index=False)
    output_files.append(output_csv)
    logger.info("SKY data by date saved to %s", output_csv)

    output_csv = OUTPUT_DIR / "ranking.csv"
    df_ranking.to_csv(output_csv, index=False)
    output_files.append(output_csv)
    logger.info("Ranking data saved to %s", output_csv)

    # Daily Data — uses df_ranking (independent of df).
    if workbook is not None:
        try:
            sheets.write_daily_data(workbook, period, df_ranking)
            logger.info("Daily Data written to workbook for %s", period)
        except (RuntimeError, gspread.exceptions.APIError) as e:
            logger.error("Could not write Daily Data to tab: %s", e)

    output_csv = OUTPUT_DIR / "vote_participation_final_transposed.csv"
    df.to_csv(output_csv, header=False, index=True)
    output_files.append(output_csv)
    logger.info("(transposed) Participation vote data saved to %s", output_csv)

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


def _run_finalize(args: argparse.Namespace) -> None:
    """Execute the finalize stage: read communication, compute metrics, write Compensation tab."""

    period = args.month
    logger.info("Finalize requested for %s", period)
    raise SystemExit("`finalize` is not yet implemented.")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m%d %H-%M-%S",
    )
    # Set Dune to WARNING to reduce terminal noise
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
