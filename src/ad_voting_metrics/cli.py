"""Command-line entry point with `fetch` and `finalize` subcommands.

`fetch` pulls SKY delegations from Dune and poll/spell vote data from vote.sky.money, then writes the raw participation
tabs to the workbook. `finalize` reads the operator-reviewed Communication Master tab and writes the Compensation tab.
"""

import argparse
import logging
from datetime import UTC, date, datetime, timedelta

from dotenv import load_dotenv

from .period import MonthPeriod
from .pipeline import run_fetch, run_finalize


def parse_month(value: str) -> MonthPeriod:
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

    today = datetime.now(UTC).date()
    if period.start > today:
        msg = (
            f"{value!r} resolves to {period.start.isoformat()}..{period.end.isoformat()}, "
            f"which is entirely in the future."
        )
        raise argparse.ArgumentTypeError(msg)

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
        msg = f"{value!r} is not an integer"
        raise argparse.ArgumentTypeError(msg) from e
    if hours < 0:
        msg = f"{value!r} is negative; --cache-hours must be 0 or greater"
        raise argparse.ArgumentTypeError(msg)
    return hours


def build_arg_parser() -> argparse.ArgumentParser:
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
            "(and CSVs to output_data/). "
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
        help=("Month to query, e.g. 'April 2026' or '2026-04'. Resolves to the full calendar month."),
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
        help=("Month to finalize, e.g. 'April 2026' or '2026-04'. Must match a month previously processed by `fetch`."),
    )

    return parser


def check_period_has_ended(period: MonthPeriod, today: date) -> None:
    """Raise SystemExit if the period hasn't ended on or before today.

    Metrics for an in-progress period are unreliable: poll close-day rules can't be applied to polls still in their
    voting window, and the SKY-ranking snapshot is incomplete. `today` should be the current UTC date, not local -
    periods are UTC-anchored (polls close at 16:00 UTC). Pass `datetime.now(UTC).date()`, not `date.today()`.

    Takes `today` as a parameter so tests can pin a deterministic clock.

    Raises:
        SystemExit: if the period's last day is on or after today.
    """
    if today <= period.end:
        next_day = period.end + timedelta(days=1)
        msg = (
            f"Refusing to compute metrics for {period}: the period has not yet "
            f"ended (UTC date is {today.isoformat()}, period ends "
            f"{period.end.isoformat()}). Re-run on or after "
            f"{next_day.isoformat()} UTC."
        )
        raise SystemExit(msg)


def main(argv: list[str] | None = None) -> None:
    """Entry point: configure logging, parse argv, dispatch to the chosen subcommand.

    SystemExit propagates from `check_period_has_ended`, from argparse on a bad command line, and from `run_fetch` /
    `run_finalize` when a required step (workbook open, compute, etc.) fails.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("dune_client").setLevel(logging.WARNING)
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    check_period_has_ended(args.month, today=datetime.now(UTC).date())

    if args.command == "fetch":
        run_fetch(args)
    elif args.command == "finalize":
        run_finalize(args)
