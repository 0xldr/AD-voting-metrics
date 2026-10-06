"""Roster of aligned delegates, loaded from delegates.yaml.

The YAML is the source of truth. Each entry:

- name: display name
- vote_delegate_address: on-chain vote delegate contract (lowercase 0x...)
- start_date: when AD compensation begins (not the contract creation date)
- end_date: optional, inclusive last day of alignment

Drift detection: every YAML entry with end_date=None should be in the API's currently-aligned response, and vice-versa.
Mismatches produce warnings - typically the YAML needs updating after a new alignment or an exit.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, Field, StringConstraints, model_validator

from .period import MonthPeriod

logger = logging.getLogger(__name__)


class Delegate(BaseModel):
    """A single AD entry, currently or previously active."""

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    vote_delegate_address: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    start_date: date
    end_date: date | None = None

    @model_validator(mode="after")
    def _end_date_after_start_date(self) -> Delegate:
        if self.end_date is not None and self.end_date <= self.start_date:
            msg = f"end_date {self.end_date} must be after start_date {self.start_date} for delegate {self.name}"
            raise ValueError(msg)
        return self

    def is_active_during(self, period: MonthPeriod) -> bool:
        """True if this delegate's alignment overlaps the period; end_date is inclusive and None means still active."""
        if self.start_date > period.end:
            return False
        return self.end_date is None or self.end_date >= period.start


class DelegatesConfig(BaseModel):
    """Top-level YAML structure: a list of delegates."""

    delegates: list[Delegate]

    @model_validator(mode="after")
    def _no_duplicate_addresses(self) -> DelegatesConfig:
        seen: dict[str, str] = {}
        for d in self.delegates:
            if d.vote_delegate_address in seen:
                msg = (
                    f"Duplicate vote_delegate_address {d.vote_delegate_address} for "
                    f"{d.name} and {seen[d.vote_delegate_address]}"
                )
                raise ValueError(msg)
            seen[d.vote_delegate_address] = d.name
        return self


def load_delegates(path: Path) -> DelegatesConfig:
    """Load and validate the delegates YAML.

    Raises:
        ValueError: if YAML is empty.

    Notes:
        The function may raise FileNotFoundError, yaml.YAMLError, or pydantic.ValidationError from validation.
    """
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        msg = f"{path} is empty or contains only YAML null"
        raise ValueError(msg)
    return DelegatesConfig.model_validate(raw)


def detect_roster_drift(
    yaml_config: DelegatesConfig,
    api_response: list[dict[str, Any]],
) -> list[str]:
    """Verify the YAML roster against the API response and return drift warnings.

    Drift rules:
    - YAML active, API absent -> warn (likely missed an exit update)
    - YAML exited, API absent -> expected, no warn
    - YAML exited, API present -> warn (date mismatch)
    - API present, not in YAML -> warn (new delegate missing from YAML)

    Comparisons are by vote delegate address, with the API side lowercased; names that differ but addresses match are
    not flagged. The YAML is the canonical roster — the API never adds anyone — so the delegate list itself is not
    returned.
    """
    warnings: list[str] = []

    yaml_by_address: dict[str, Delegate] = {d.vote_delegate_address: d for d in yaml_config.delegates}
    api_by_address = {entry["voteDelegateAddress"].lower(): entry for entry in api_response}

    for addr, delegate in yaml_by_address.items():
        in_api = addr in api_by_address
        if delegate.end_date is None and not in_api:
            warnings.append(
                f"{delegate.name} ({addr}) is marked active in YAML "
                f"(end_date=null) but does not appear in the API as currently "
                f"aligned. Did they exit? Update end_date in delegates.yaml",
            )
        elif delegate.end_date is not None and in_api:
            warnings.append(
                f"{delegate.name} ({addr}) is marked exited in YAML "
                f"(endDate={delegate.end_date}) but the API still shows them "
                f"as currently aligned. Date mismatch - verify which is correct.",
            )

    for addr, entry in api_by_address.items():
        if addr not in yaml_by_address:
            api_name = entry.get("name", "?")
            warnings.append(
                f"{api_name} ({addr}) appears in the API as currently "
                "aligned but is not in delegates.yaml. Add an entry.",
            )

    return warnings


@dataclass
class RosterResult:
    """Outcome of build_roster_for_period.

    Carries the active-delegate list plus metadata for the reconciliation log.
    """

    active_delegates: list[Delegate]
    drift_warnings: list[str]
    yaml_config: DelegatesConfig  # full config so callers can split active/exited
    api_delegate_count: int | None  # None when the API fetch failed and drift detection was skipped


def build_roster_for_period(
    yaml_path: Path,
    period: MonthPeriod,
    api_fetcher: Callable[[], list[dict[str, Any]]],
) -> RosterResult:
    """Load YAML, fetch the API, run drift detection, filter to active-during-period.

    Drift detection compares the YAML against the live vote.sky.money listing. A fetch failure degrades to YAML-only
    with a warning rather than aborting the run.
    """
    yaml_config = load_delegates(yaml_path)

    api_delegate_count: int | None
    try:
        api_response = api_fetcher()
    except Exception as e:  # noqa: BLE001 — api_fetcher is caller-supplied; any failure degrades to YAML-only
        logger.warning("API fetch failed during drift check: %s", e)
        skipped = f"API drift check skipped due to fetch failure: {type(e).__name__}: {e}."
        warnings = [f"{skipped} Proceeding with delegates.yaml as the sole source."]
        api_delegate_count = None
    else:
        warnings = detect_roster_drift(yaml_config, api_response)
        api_delegate_count = len(api_response)

    return RosterResult(
        active_delegates=[d for d in yaml_config.delegates if d.is_active_during(period)],
        drift_warnings=warnings,
        yaml_config=yaml_config,
        api_delegate_count=api_delegate_count,
    )
