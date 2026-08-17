"""Per-run reconciliation log.

Each successful run writes one JSON file to output_data/reconciliation/, named <YYYY-MM>_<UTC-timestamp>.json — the
period queried first, the run timestamp second, both sortable.

The log captures structured metadata about a run: YAML and API delegate counts, whether the API call succeeded, drift
warnings, the on-chain delegation sync state, and which files were produced. Lets operators answer "what happened during
this run" without re-running.

Writing is soft-fail: if the log can't be written (permission denied, disk full, etc.), a warning is logged and the
script continues. CSV outputs are the primary artifacts; the log is supplementary.
"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .period import MonthPeriod
from .roster import RosterResult

logger = logging.getLogger(__name__)


class ReconciliationEntry(TypedDict):
    """The schema of a reconciliation entry.

    All fields are JSON-serializable: timestamps are ISO 8601 in UTC strings; paths are strings, counts are non-negative
    ints. The TypedDict gives mypy a name to check the shape at construction sites.
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
    delegation_source: str
    delegation_factory_block: int
    delegation_last_synced_block: int
    output_files: list[str]


def build_entry(
    *,
    period: MonthPeriod,
    yaml_path: Path,
    roster: RosterResult,
    delegation: dict[str, int],
    output_files: list[Path],
) -> ReconciliationEntry:
    """Construct a reconciliation log entry from this run's facts.

    `delegation` is the on-chain sync state {factory_block, last_synced_block}. `delegation_source` is always 'onchain';
    the field is retained so entries written before it became constant have the same shape as new ones.

    `roster.api_delegate_count` is 0 when api_fetch_succeeded is False (the API call raised and was soft-failed).

    All fields are JSON-serializable. The keyword-only signature reflects the call pattern in pipeline.py and prevents
    accidental positional calls.

    Returns:
        A populated ReconciliationEntry.
    """
    yaml_config = roster.yaml_config
    yaml_active = sum(1 for d in yaml_config.delegates if d.end_date is None)
    yaml_exited = sum(1 for d in yaml_config.delegates if d.end_date is not None)

    return {
        "run_timestamp": datetime.now(UTC).isoformat(),
        "period": str(period),
        "period_start": period.start.isoformat(),
        "period_end": period.end.isoformat(),
        "yaml_path": str(yaml_path),
        "yaml_total_delegates": len(yaml_config.delegates),
        "yaml_active_delegates": yaml_active,
        "yaml_exited_delegates": yaml_exited,
        "api_delegate_count": roster.api_delegate_count,
        "api_fetch_succeeded": roster.api_fetch_succeeded,
        "active_during_period": len(roster.active_delegates),
        "drift_warnings": list(roster.drift_warnings),
        "delegation_source": "onchain",
        "delegation_factory_block": delegation["factory_block"],
        "delegation_last_synced_block": delegation["last_synced_block"],
        "output_files": [str(p) for p in output_files],
    }


def write_entry(directory: Path, period: MonthPeriod, entry: ReconciliationEntry) -> Path | None:
    """Write a reconciliation entry as a JSON file under `directory`.

    Filename format `<YYYY-MM>_<sanitized-iso-timestamp>.json` combines the period and the entry's run timestamp so
    re-runs of the same period produce distinct files. Colons are hyphens (illegal on Windows) and `+00:00` collapses
    to `Z` for compactness.

    Soft-fails on any IO error: logs a warning and returns None rather than blocking the run.

    Returns:
        The written path on success, or None on failure.
    """
    period_iso = period.start.strftime("%Y-%m")
    sanitized_ts = entry["run_timestamp"].replace("+00:00", "Z").replace(":", "-")
    try:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{period_iso}_{sanitized_ts}.json"
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
