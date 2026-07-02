"""JSON file-cache helpers shared by the on-chain data modules.

The delegation event cache and the slate cache are both persistent JSON dicts under output_data/; this module owns
the disk format (pretty-printed, sorted keys) and the write discipline. Domain-specific normalization (address
lowercasing) stays with each cache's loader.
"""

import json
from pathlib import Path
from typing import Any


def load_json_cache(path: Path) -> dict[str, Any]:
    """Load a JSON cache file.

    Returns:
        The parsed dict, or an empty dict if the file doesn't exist.
    """
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data: dict[str, Any] = json.load(f)
    return data


def save_json_cache(data: dict[str, Any], path: Path) -> None:
    """Persist a cache dict as JSON, atomically, creating parent dirs if needed.

    Writes to a temp file in the same directory and renames it over the target, so a crash mid-write leaves the
    previous cache intact instead of a truncated file (which would force a full resync).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
