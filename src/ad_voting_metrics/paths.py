"""Filesystem locations shared across the pipeline.

All data paths hang off the repo root, resolved relative to this file. This assumes the package runs from a source
checkout (the documented workflow); under a wheel install these paths would resolve into site-packages, so keep the
tool checkout-run until output locations become configurable.
"""

from pathlib import Path

from .period import MonthPeriod

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# delegates.yaml — the canonical roster, at the repo root.
YAML_PATH = REPO_ROOT / "delegates.yaml"

# Everything the pipeline writes at runtime lives under output_data/.
OUTPUT_DIR = REPO_ROOT / "output_data"
RECONCILIATION_LOG_PATH = OUTPUT_DIR / "reconciliation"
DELEGATION_CACHE_PATH = OUTPUT_DIR / "delegation_cache.json"
SLATE_CACHE_PATH = OUTPUT_DIR / "slate_cache.json"


def output_dir_for(period: MonthPeriod) -> Path:
    """Return the per-month output directory, e.g. output_data/2026-04."""
    return OUTPUT_DIR / period.start.strftime("%Y-%m")
