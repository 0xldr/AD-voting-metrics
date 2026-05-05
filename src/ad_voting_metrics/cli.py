import argparse
import logging
from datetime import date
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from . import sky_dao as sky
from .period import MonthPeriod

logger = logging.getLogger(__name__)

# Locate data and output directories relative to the repo root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = REPO_ROOT / "delegate_date"
OUTPUT_DIR = REPO_ROOT / "output_data"


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


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="ad-voting-metrics",
        description=(
            "Generate AD voting metrics CSVs for a single month. "
            "Reads the aligned-delegate roster, fetches SKY delegations and "
            "poll/spell vote data, and writes CSVs to output_data/."
        ),
    )
    parser.add_argument(
        "--month",
        required=True,
        type=parse_month,
        metavar="MONTH",
        help=(
            "Month to query, e.g. 'April 2026' or '2026-04'. Resolves to the full calendar month."
        ),
    )
    return parser


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    period = args.month

    logger.info(
        "Querying %s (%s through %s)", period, period.start.isoformat(), period.end.isoformat()
    )

    # Step 1: Read the delegate roster
    file_path = DATA_DIR / "Aligned Delegates v3.csv"
    df = pd.read_csv(file_path)

    hardcoded_order = df["Delegate Contract"].str.lower().tolist()

    # Get delegate list and SKY ranking
    logger.info("Getting RANKING...")
    delegate_list_sky, delegate_list_rank = sky.get_delegate_list_sky(df, period)

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
    df = sky.get_vote_poll_ids(POLL_INFO, df, df_sky)

    # Get SPELL addresses information and vote from SPELL
    logger.info("Getting SPELL addresses...")
    SPELL_INFO = sky.get_execute_ids(period)
    logger.info("Getting VOTE FROM SPELL...")
    df = sky.get_vote_execute_ids(SPELL_INFO, df, df_sky)

    # Save data to CSV files
    output_csv = OUTPUT_DIR / "vote_participation.csv"
    df.to_csv(output_csv, index=False)
    logger.info("Participation vote data saved to %s", output_csv)

    df = sky.custom_sort(df, hardcoded_order, POLL_INFO, SPELL_INFO)

    output_csv = OUTPUT_DIR / "sky.csv"
    df_sky.to_csv(output_csv, index=False)
    logger.info("SKY data by date saved to %s", output_csv)

    output_csv = OUTPUT_DIR / "ranking.csv"
    df_ranking.to_csv(output_csv, index=False)
    logger.info("Ranking data saved to %s", output_csv)

    output_csv = OUTPUT_DIR / "vote_participation_final_transposed.csv"
    df.to_csv(output_csv, header=False, index=True)
    logger.info("(transposed) Participation vote data saved to %s", output_csv)
