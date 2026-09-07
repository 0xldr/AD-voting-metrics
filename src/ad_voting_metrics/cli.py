"""Command-line entry point.

Pulls SKY delegations from on-chain Lock/Free events and poll/spell vote data from vote.sky.money for a single month,
then writes the month's CSVs to output_data/<YYYY-MM>/.
"""

import argparse
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from web3 import Web3

from .period import MonthPeriod
from .pipeline import run

logger = logging.getLogger(__name__)

REDACTED = "<redacted>"


class RedactingFormatter(logging.Formatter):
    """Formatter that blanks the given secrets from every emitted line, tracebacks included.

    RPC providers put the API key in the URL path, and `requests` reproduces that path in its error messages, so the
    RPC URL and its path are the secrets to hide. Single-character values are ignored so a bare "/" path cannot blank
    every slash in the log.
    """

    def __init__(self, fmt: str, datefmt: str, secrets: list[str]) -> None:
        super().__init__(fmt, datefmt)
        self._secrets = [s for s in secrets if len(s) > 1]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(*, verbose: bool, secrets: list[str]) -> None:
    """Send INFO logs to stderr with `secrets` redacted; with verbose, this package logs at DEBUG.

    Replaces any redacting handler installed by an earlier call so repeated configuration never stacks handlers.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S", secrets))
    root = logging.getLogger()
    for existing in [h for h in root.handlers if isinstance(h.formatter, RedactingFormatter)]:
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("ad_voting_metrics").setLevel(logging.DEBUG if verbose else logging.NOTSET)


def parse_month(value: str) -> MonthPeriod:
    """Argparse type callback: parse the --month value into a MonthPeriod.

    Whether the month has ended is checked separately in `check_period_has_ended`.

    Raises:
        argparse.ArgumentTypeError: if the value is unparseable.
    """
    try:
        return MonthPeriod.from_string(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
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
        help="Discard the cached delegation events and resync from the V3 factory block instead of only new blocks.",
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
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Also log debug detail such as getLogs chunk sizing and batch fallbacks.",
    )

    return parser


def check_period_has_ended(period: MonthPeriod, today: date) -> None:
    """Raise SystemExit if the period hasn't ended on or before today.

    Metrics for an in-progress period are unreliable: poll close-day rules can't be applied to polls still in their
    voting window, and the SKY-ranking snapshot is incomplete. `today` should be the current UTC date, not local -
    periods are UTC-anchored (polls close at 16:00 UTC). Pass `datetime.now(UTC).date()`, not `date.today()`.

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


def rpc_url_from_env() -> str:
    """Return SKY_RPC_URL, the mainnet JSON-RPC endpoint used for the delegation sync and executive-vote verification.

    Raises:
        SystemExit: if SKY_RPC_URL is unset or blank.
    """
    rpc_url = os.environ.get("SKY_RPC_URL")
    if not rpc_url:
        raise SystemExit("SKY_RPC_URL environment variable is not set. Add it to your .env file (see .env.example).")
    return rpc_url


def main(argv: list[str] | None = None) -> None:
    """Entry point: parse argv, configure redacted logging, run the pipeline.

    SystemExit propagates from `check_period_has_ended`, `rpc_url_from_env`, and from argparse on a bad command line.
    A failure inside the run is logged through the redacting formatter and exits with status 1, so an RPC key embedded
    in a provider URL never reaches the terminal through an unredacted traceback.
    """
    load_dotenv()
    args = build_arg_parser().parse_args(argv)
    check_period_has_ended(args.month, today=datetime.now(UTC).date())

    rpc_url = rpc_url_from_env()
    rpc_parts = urlsplit(rpc_url)
    configure_logging(verbose=args.verbose, secrets=[rpc_url, rpc_parts.path, rpc_parts.query])

    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        run(args.month, rebuild=args.rebuild, roster_path=args.roster, output_dir=args.output_dir, w3=w3)
    except Exception:
        logger.exception("Run failed")
        raise SystemExit(1) from None
