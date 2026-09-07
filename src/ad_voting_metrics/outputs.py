"""Everything a run writes: the month's two CSVs and a reconciliation log entry.

The CSVs go into the month's output directory (output_data/<YYYY-MM>/ by default):

  - sky.csv: one row per (delegate, day) with the SKY balance and that day's rank
  - vote_participation.csv: one row per poll/spell with its metadata and each delegate's participation status

Poll and spell titles come from external APIs and the CSVs are meant to be opened in spreadsheet applications, so
formula-like cells are neutralised before writing.

The reconciliation log is one JSON file per run under <output-dir>/reconciliation/, named
<YYYY-MM>_<UTC-timestamp>.json so files sort by period, then by run. It records roster and API delegate counts, drift
warnings, the on-chain sync head, and the files produced, so an operator can answer "what happened during this run"
without re-running. Writing it is soft-fail: the CSVs are the primary artifacts and the log is supplementary.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

import pandas as pd

from .ballots import Ballot, Statuses
from .period import MonthPeriod
from .roster import Delegate, RosterResult

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
    left blank. The sort is stable, so ballots sharing a start date keep their input order. With no ballots the frame
    is header-only.
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


def write_csvs(out_dir: Path, daily: pd.DataFrame, participation: pd.DataFrame) -> list[Path]:
    """Write sky.csv and vote_participation.csv into out_dir, creating it if needed, and return their paths.

    `daily` is the ranked per-(delegate, day) balance frame and `participation` the wide ballot table from
    `build_participation_dataframe`. Re-runs overwrite the same files.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    sky_csv = out_dir / "sky.csv"
    daily.to_csv(sky_csv, index=False)
    logger.info("Daily SKY balances saved to %s", sky_csv)

    participation_csv = out_dir / "vote_participation.csv"
    _defuse_csv_formulas(participation).to_csv(participation_csv, index=False)
    logger.info("Participation statuses saved to %s", participation_csv)

    return [sky_csv, participation_csv]


class ReconciliationEntry(TypedDict):
    """One run's facts, as written to the reconciliation log.

    `run_timestamp` is ISO 8601 in UTC and `period` is the human form ("April 2026"). `roster_delegates` counts every
    entry in the roster YAML, `roster_active_delegates` those with no end_date, and `active_during_period` those the
    run reported on. `api_delegate_count` is None when the vote.sky.money fetch failed and drift detection was skipped.
    """

    run_timestamp: str
    period: str
    roster_path: str
    roster_delegates: int
    roster_active_delegates: int
    active_during_period: int
    api_delegate_count: int | None
    drift_warnings: list[str]
    last_synced_block: int
    output_files: list[str]


def build_reconciliation_entry(
    *,
    period: MonthPeriod,
    roster_path: Path,
    roster: RosterResult,
    last_synced_block: int,
    output_files: list[Path],
) -> ReconciliationEntry:
    """Construct the reconciliation entry for this run; `last_synced_block` is the delegation sync head after it.

    Paths are recorded absolute so the entry stays meaningful when read from another working directory.
    """
    yaml_delegates = roster.yaml_config.delegates
    return {
        "run_timestamp": datetime.now(UTC).isoformat(),
        "period": str(period),
        "roster_path": str(roster_path.resolve()),
        "roster_delegates": len(yaml_delegates),
        "roster_active_delegates": sum(1 for d in yaml_delegates if d.end_date is None),
        "active_during_period": len(roster.active_delegates),
        "api_delegate_count": roster.api_delegate_count,
        "drift_warnings": list(roster.drift_warnings),
        "last_synced_block": last_synced_block,
        "output_files": [str(p.resolve()) for p in output_files],
    }


def write_reconciliation_entry(directory: Path, period: MonthPeriod, entry: ReconciliationEntry) -> Path | None:
    """Write a reconciliation entry as `<YYYY-MM>_<timestamp>.json` under `directory` and return its path.

    The timestamp is the entry's run_timestamp with colons replaced by hyphens (illegal on Windows) and `+00:00`
    collapsed to `Z`, so re-runs of the same period produce distinct files. Soft-fails on any IO error: logs a warning
    and returns None rather than blocking the run.
    """
    period_iso = period.start.strftime("%Y-%m")
    sanitized_ts = entry["run_timestamp"].replace("+00:00", "Z").replace(":", "-")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{period_iso}_{sanitized_ts}.json"
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
        logger.info("Reconciliation log written to %s", path)
    except OSError as e:
        logger.warning(
            "Failed to write reconciliation log to %s: %s: %s. Run output is unaffected.",
            directory,
            type(e).__name__,
            e,
        )
        return None
    else:
        return path
