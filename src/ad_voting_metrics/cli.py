"""Command-line entry point.

Pulls SKY delegations from on-chain Lock/Free events and poll/spell vote data from vote.sky.money for a single month,
then writes the month's CSVs to output_data/<YYYY-MM>/.
"""

import argparse
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

from .period import MonthPeriod
from .pipeline import run


def parse_month(value: str) -> MonthPeriod:
    """Argparse type callback: parse the --month value into a MonthPeriod.

    Whether the month has ended is checked separately in `check_period_has_ended`.

    Returns:
        The parsed MonthPeriod.

    Raises:
        argparse.ArgumentTypeError: if the value is unparseable.
    """
    try:
        return MonthPeriod.from_string(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="ad-voting-metrics",
        description=(
            "Generate AD voting metrics for a single month. Pulls SKY "
            "delegations from on-chain Lock/Free events and poll/spell vote "
            "data from vote.sky.money, computes participation status per "
            "(poll, delegate), and writes the month's CSVs to output_data/."
        ),
    )
    parser.add_argument(
        "--month",
        required=True,
        type=parse_month,
        metavar="MONTH",
        help=("Month to query, e.g. 'April 2026' or '2026-04'. Resolves to the full calendar month."),
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Force a full resync from the V3 factory block, discarding cached events. "
            "By default, syncs only new blocks since the last run (fast). "
            "Use --rebuild to rebuild the entire delegation history."
        ),
    )
    parser.add_argument(
        "--roster",
        type=Path,
        default=Path("delegates.yaml"),
        metavar="FILE",
        help="Delegate roster YAML (default: %(default)s, relative to the working directory).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output_data"),
        metavar="DIR",
        help="Directory for per-month CSVs, on-chain caches and reconciliation logs (default: %(default)s).",
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
    """Entry point: configure logging, parse argv, run the pipeline.

    SystemExit propagates from `check_period_has_ended` and from argparse on a bad command line.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    check_period_has_ended(args.month, today=datetime.now(UTC).date())

    run(args.month, rebuild=args.rebuild, roster_path=args.roster, output_dir=args.output_dir)
