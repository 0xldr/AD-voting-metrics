"""CSV outputs for one run.

Everything the pipeline writes for a month lands in output_data/<YYYY-MM>/:

  - sky.csv: one row per (delegate, day) with the SKY balance and that day's rank
  - vote_participation.csv: one row per poll/spell with its metadata and each delegate's participation status

Poll and spell titles come from external APIs and the CSVs are meant to be opened in spreadsheet applications, so
formula-like cells are neutralised before writing.
"""

import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .paths import output_dir_for
from .period import MonthPeriod
from .roster import ROSTER_COLUMNS

logger = logging.getLogger(__name__)

PARTICIPATION_METADATA_COLUMNS: tuple[str, ...] = ("Poll Id", "Start Date", "End Date", "Title")

# Leading characters that spreadsheet applications interpret as a formula (or,
# for \t and \r, as field separators that can smuggle one in).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _coerce_date(value: date | datetime | None) -> str:
    """Render a date, datetime or pd.Timestamp as 'YYYY-MM-DD'; None becomes ''."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def _metadata_by_id(poll_info: list[dict], spell_info: list[dict]) -> dict[str, dict]:
    """Index poll and spell records by stringified ID/address.

    Returns a mapping of str(pollId) / str(address) to its record. Polls win an (unexpected) key collision.
    """
    out: dict[str, dict] = {str(spell["address"]): spell for spell in spell_info}
    out.update({str(poll["pollId"]): poll for poll in poll_info})
    return out


def build_participation_dataframe(
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> pd.DataFrame:
    """Build the wide-format participation table.

    Input df has ROSTER_COLUMNS plus one column per poll id (str) and one per spell address, one row per delegate.

    Output has one row per poll/spell with columns [Poll Id, Start Date, End Date, Title, <Delegate 1>, ...]. Zero-poll
    months return a header-only DataFrame.

    Poll/spell columns with no matching poll_info or spell_info entry get blank metadata cells; the status column is
    still written so a transient API inconsistency can't drop participation data. Spell rows have a blank End Date.

    Returns:
        Wide-format participation DataFrame.
    """
    delegate_names = df["Delegate Name"].tolist()
    poll_columns = [c for c in df.columns if c not in ROSTER_COLUMNS]
    columns = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]

    if not poll_columns:
        return pd.DataFrame(columns=columns)

    df_by_delegate = df.set_index("Delegate Name")
    metadata_by_id = _metadata_by_id(poll_info, spell_info)
    rows: list[dict] = []
    for poll_id in poll_columns:
        metadata = metadata_by_id.get(str(poll_id), {})
        row: dict = {
            "Poll Id": str(poll_id),
            "Start Date": _coerce_date(metadata.get("startDate")),
            "End Date": _coerce_date(metadata.get("endDate")),
            "Title": str(metadata.get("title", "")),
        }
        for name in delegate_names:
            row[name] = str(df_by_delegate.loc[name, poll_id])
        rows.append(row)

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
    period: MonthPeriod,
    daily: pd.DataFrame,
    metrics: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> list[Path]:
    """Write sky.csv and vote_participation.csv into the period's output directory.

    `daily` is the ranked per-(delegate, day) balance frame; `metrics` is the roster frame with one status column per
    poll/spell. Re-runs overwrite the same month's files.

    Returns:
        The CSV paths written, in write order.
    """
    out_dir = output_dir_for(period)
    out_dir.mkdir(parents=True, exist_ok=True)

    sky_csv = out_dir / "sky.csv"
    daily.to_csv(sky_csv, index=False)
    logger.info("Daily SKY balances saved to %s", sky_csv)

    participation_csv = out_dir / "vote_participation.csv"
    participation = build_participation_dataframe(metrics, poll_info, spell_info)
    _defuse_csv_formulas(participation).to_csv(participation_csv, index=False)
    logger.info("Participation statuses saved to %s", participation_csv)

    return [sky_csv, participation_csv]
