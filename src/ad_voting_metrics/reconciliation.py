"""Per-run reconciliation log.

Each successful run writes one JSON file to output_data/reconciliation/,
named <YYYY-MM>_<UTC-timestamp>.json — the period queried first, the run
timestamp second, both sortable.

The log captures structured metadata about a run: YAML and API delegate
counts, whether the API call succeeded, drift warnings, which Dune query
was executed, amd which files were produced. Lets operators answer
"what happened during this run" without re-running.

Writing is soft-fail: if the log can't be written (permission denied,
disk full, etc.), a warning is logged and the script continues. CSV
outputs are the primary artifacts; the log is supplementary.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .period import MonthPeriod
from .roster import Delegate, DelegatesConfig

logger = logging.getLogger(__name__)


class ReconciliationEntry(TypedDict):
    """The schema of a reconciliation entry.

    All fields are JSON-serializable: timestamps are ISO 8601 in UTC
    strings; paths are strings, counts are non-negative ints. The
    TypedDict gives mypy a name to check the shape at construction sites.
    """

    run_timestamp: str
    period: str
    period_start: str
    period_end: str
    yaml_path: str
    yaml_total_delegates: int
    yaml_active_delegates: int
    yaml_exited_delegates: int
    api_delegate_count: int
    api_fetch_succeeded: bool
    active_during_period: int
    drift_warnings: list[str]
    dune_query_id: int
    dune_execution_mode: str
    dune_cache_max_age_hours: int | None
    output_files: list[str]


def build_entry(
    *,
    period: MonthPeriod,
    yaml_path: Path,
    yaml_config: DelegatesConfig,
    active_delegates: list[Delegate],
    drift_warnings: list[str],
    dune_query_id: int,
    dune_cache_max_age_hours: int | None,
    api_delegate_count: int,
    api_fetch_succeeded: bool,
    output_files: list[Path],
) -> ReconciliationEntry:
    """Construct a reconciliation log entry from this run's facts.

    dune_cache_max_age_hours is None when Dune was run fresh, or an
    integer N when --cache-hours N was used.

    api_delegate_count is 0 when api_fetch_succeeded is False (the
    API call raised and was soft-failed).

    All fields are JSON-serializable. The keyword-only signature reflects
    the call pattern in cli.py and prevents accidental positional calls.

    Returns:
        A populated ReconciliationEntry.
    """
    yaml_active = sum(1 for d in yaml_config.delegates if d.end_date is None)
    yaml_exited = sum(1 for d in yaml_config.delegates if d.end_date is not None)

    dune_execution_mode = "fresh" if dune_cache_max_age_hours is None else "cached"

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
        "dune_execution_mode": dune_execution_mode,
        "dune_cache_max_age_hours": dune_cache_max_age_hours,
        "output_files": [str(p) for p in output_files],
    }


def _filename(period: MonthPeriod, run_timestamp_iso: str) -> str:
    """Build a filesystem-safe, sortable filename for a single run.

    Format: <YYYY-MM>_<YYYY-MM-DDTHH-MM-SSZ>.json (e.g.
    "2026-04_2026-05-06T15-32-08Z.json). Colons are replaced with
    hyphens because they're illegal on Windows filesystems

    Returns:
        The composed filename.
    """
    period_iso = period.start.strftime("%Y-%m")
    dt = datetime.fromisoformat(run_timestamp_iso)
    timestamp = dt.strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{period_iso}_{timestamp}.json"


def write_entry(directory: Path, period: MonthPeriod, entry: ReconciliationEntry) -> Path | None:
    """Write a reconciliation entry as a JSON file under `directory`.

    Soft-fails on any IO error: logs a warning and returns None rather
    than blocking the run.

    The filename combines the period and the entry's run timestamp, so
    re-runs of the same period produce distinct files rather than
    overwriting.

    Returns:
        The written path on success, or None on failure.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / _filename(period, entry["run_timestamp"])
        path.write_text(json.dumps(entry, indent=2, ensure_ascii=False))
        logger.info("Reconciliation log written to %s", path)
    except (OSError, TypeError, ValueError) as e:
        logger.warning(
            "Failed to write reconciliation log to %s: %s: %s. Run output is unaffected.",
            directory,
            type(e).__name__,
            e,
        )
        return None
    else:
        return path
