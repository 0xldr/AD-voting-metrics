"""CSV outputs for one run.

The pipeline writes two files into the month's output directory (output_data/<YYYY-MM>/ by default):

  - sky.csv: one row per (delegate, day) with the SKY balance and that day's rank
  - vote_participation.csv: one row per poll/spell with its metadata and each delegate's participation status

Poll and spell titles come from external APIs and the CSVs are meant to be opened in spreadsheet applications, so
formula-like cells are neutralised before writing.
"""

import logging
from pathlib import Path

import pandas as pd

from .ballot import Ballot
from .roster import Delegate
from .vote_status import Statuses

logger = logging.getLogger(__name__)

PARTICIPATION_METADATA_COLUMNS: tuple[str, ...] = ("Poll Id", "Start Date", "End Date", "Title")

# Leading characters that spreadsheet applications interpret as a formula (or,
# for \t and \r, as field separators that can smuggle one in).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def build_participation_dataframe(
    delegates: list[Delegate],
    ballots: list[Ballot],
    statuses: Statuses,
) -> pd.DataFrame:
    """Build the wide participation table: one row per ballot, sorted by start date, one column per delegate.

    Columns are PARTICIPATION_METADATA_COLUMNS followed by the delegate names in roster order. The id column keeps the
    "Poll Id" header for spells too. Spell rows have a blank End Date. A (delegate, ballot) pair with no status is
    left blank. The sort is stable, so ballots sharing a start date keep their input order.

    Returns:
        Wide-format participation DataFrame; header-only when there are no ballots.
    """
    columns = [*PARTICIPATION_METADATA_COLUMNS, *(d.name for d in delegates)]
    rows = [
        {
            "Poll Id": ballot.id,
            "Start Date": ballot.start.isoformat(),
            "End Date": ballot.end.isoformat() if ballot.end else "",
            "Title": ballot.title,
            **{d.name: statuses.get((d.vote_delegate_address, ballot.id), "") for d in delegates},
        }
        for ballot in sorted(ballots, key=lambda b: b.start)
    ]
    return pd.DataFrame(rows, columns=columns)


def _defuse_csv_formulas(df: pd.DataFrame) -> pd.DataFrame:
    """Prefix formula-like string cells with an apostrophe so spreadsheet apps treat them as text.

    A title like "=IMPORTDATA(...)" must not execute when the CSV is opened. The apostrophe is the spreadsheet
    convention for literal text; apps hide it on display.

    Returns a copy of df; non-string cells are unchanged.
    """

    def defuse(value: object) -> object:
        if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
            return f"'{value}"
        return value

    return df.map(defuse)


def write_csvs(
    out_dir: Path,
    daily: pd.DataFrame,
    delegates: list[Delegate],
    ballots: list[Ballot],
    statuses: Statuses,
) -> list[Path]:
    """Write sky.csv and vote_participation.csv into out_dir, creating it if needed.

    `daily` is the ranked per-(delegate, day) balance frame. Re-runs overwrite the same files.

    Returns:
        The CSV paths written, in write order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    sky_csv = out_dir / "sky.csv"
    daily.to_csv(sky_csv, index=False)
    logger.info("Daily SKY balances saved to %s", sky_csv)

    participation_csv = out_dir / "vote_participation.csv"
    participation = build_participation_dataframe(delegates, ballots, statuses)
    _defuse_csv_formulas(participation).to_csv(participation_csv, index=False)
    logger.info("Participation statuses saved to %s", participation_csv)

    return [sky_csv, participation_csv]
