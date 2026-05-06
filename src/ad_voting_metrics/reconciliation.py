"""Per-run reconciliation log.

Each successful run writes one JSON file to output_data/reconciliation/,
named <YYYY-MM>_<UTC-timestamp>.json — the period queried first, the run
timestamp second, both sortable.

The log captures structured metadata about a run: how many delegates
were in the YAML and the API, whether the API call succeeded, what
drift was detected, which Dune query was executed, what files were
produced. Operators (or future tooling) can answer "what happened
during this run" without re-running.

Phase 4b will likely teach the Sheets writer to read these files and
append entries to a workbook tab; for now they're operator-facing
artifacts.

Soft-fail: if writing the log throws (permission denied, disk full,
serialisation error), a stderr warning is logged but the script
continues. The CSV outputs are the primary artifacts of a run; the
reconciliation file is supplementary.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from .period import MonthPeriod
from .roster import Delegate, DelegatesConfig

logger = logging.getLogger(__name__)


def build_entry(
    period: MonthPeriod,
    yaml_path: Path,
    yaml_config: DelegatesConfig,
    active_delegates: list[Delegate],
    drift_warnings: list[str],
    dune_query_id: int,
    api_delegate_count: int,
    api_fetch_succeeded: bool,
    output_files: list[Path],
) -> dict:
    """Construct a reconciliation log entry from this run's facts.

    All fields are JSON-serialisable. Timestamps are ISO 8601 in UTC.
    Counts are integers. Drift warnings are the same list cli.py logs
    to stderr — including them here gives a structured record of every
    warning fired during the run.

    api_delegate_count is the count from vote.sky.money's response, or
    0 if api_fetch_succeeded is False (i.e. the API call raised and we
    soft-failed). api_fetch_succeeded is the boolean we'd otherwise
    have to infer from the drift_warnings string contents.

    output_files is the list of CSV paths written by the run. Lets the
    log point at exactly which artifacts this entry corresponds to.
    """
    yaml_active = sum(1 for d in yaml_config.delegates if d.endDate is None)
    yaml_exited = sum(1 for d in yaml_config.delegates if d.endDate is not None)

    return {
        "run_timestamp": datetime.now(UTC).isoformat(),
        "period": str(period),
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "yaml_path": str(yaml_path),
        "yaml_total_delegates": len(yaml_config.delegates),
        "yaml_active_delegates": yaml_active,
        "yaml_exited_delegates": yaml_exited,
        "api_delegate_count": api_delegate_count,
        "api_fetch_succeeded": api_fetch_succeeded,
        "active_during_period": len(active_delegates),
        "drift_warnings": list(drift_warnings),
        "dune_query_id": dune_query_id,
        "output_files": [str(p) for p in output_files],
    }


def _filename(period: MonthPeriod, run_timestamp_iso: str) -> str:
    """Build a filesystem-safe, sortable filename for a single run.

    Format: <YYYY-MM>_<YYYY-MM-DDTHH-MM-SSZ>.json

    Example: 2026-04_2026-05-06T15-32-08Z.json

    Colons in the time component are replaced with hyphens because they
    are illegal on Windows filesystems and confuse some shells. The
    leading period component (YYYY-MM) makes ls-sort group by period;
    the timestamp suffix disambiguates re-runs of the same period.

    The run_timestamp_iso is the ISO 8601 string already produced by
    build_entry, so the filename and the file's run_timestamp field
    refer to exactly the same moment.
    """
    period_iso = period.start.strftime("%Y-%m")
    # Strip microseconds and timezone offset notation, replace colons.
    # Input shape: "2026-05-06T15:32:08.123456+00:00" or "...+00:00"
    # Output shape: "2026-05-06T15-32-08Z"
    dt = datetime.fromisoformat(run_timestamp_iso)
    timestamp = dt.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{period_iso}_{timestamp}.json"


def write_entry(directory: Path, period: MonthPeriod, entry: dict) -> Path | None:
    """Write a reconciliation entry as a single JSON file in `directory`.

    Returns the written path on success, None on failure. Soft-fails: if
    creating the directory or writing the file raises, logs a WARNING
    and returns None. CSV outputs are primary; this log is supplementary
    and must not block a successful run.

    The filename combines the period and the run timestamp from the
    entry's run_timestamp field, so re-runs of the same period produce
    distinct files rather than overwriting.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _filename(period, entry["run_timestamp"])
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
        logger.info("Reconciliation log written to %s", path)
        return path
    except Exception as e:
        logger.warning(
            "Failed to write reconciliation log to %s: %s: %s. Run output is unaffected.",
            directory,
            type(e).__name__,
            e,
        )
        return None
