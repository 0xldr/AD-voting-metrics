"""Roster of aligned delegates, loaded from delegates.yaml.

The YAML is the source of truth. Each entry:

- name: display name
- vote_delegate_address: on-chain vote delegate contract (lowercase 0x...)
- start_date: when AD compensation begins (not the contract creation date)
- end_date: optional, inclusive last day of alignment
- levels: optional L1/L2 governance assignments (sequences allowed, no
  overlaps). Level 3 is computed daily from rank plus eligibility, never
  set in the YAML.

Drift detection: every YAML entry with end_date=None should be in the API's
currently-aligned response, and vice-versa. Mismatches produce warnings -
typically the YAML needs updating after a new alignment or an exit.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .period import MonthPeriod

logger = logging.getLogger(__name__)


class LevelAssignment(BaseModel):
    """A single L1 or L2 governance assignment.

    A delegate may have a sequence of level assignments over their lifetime
    (e.g. promoted from L2 to L1) but never two concurrent levels -
    see Delegate validation for the no-overlap enforcement. Level 3 is
    never represented here; it's computed daily.
    """

    level: int
    start_date: date
    end_date: date | None = None

    @field_validator("level")
    @classmethod
    def _level_is_1_or_2(cls, v: int) -> int:
        if v not in {1, 2}:
            msg = f"level must be 1 or 2 (got {v}); level 3 is computed daily and is never set in the YAML"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def _end_after_start(self) -> "LevelAssignment":
        if self.end_date is not None and self.end_date <= self.start_date:
            msg = f"LevelAssignment end_date {self.end_date} must be after start_date {self.start_date}"
            raise ValueError(msg)
        return self

    def covers(self, d: date) -> bool:
        """True if d falls within this assignment's period (inclusive).

        Returns:
            Whether d is in the period.
        """
        if d < self.start_date:
            return False
        return not (self.end_date is not None and d > self.end_date)


class Delegate(BaseModel):
    """A single AD entry, currently or previously active."""

    name: str
    vote_delegate_address: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    start_date: date
    end_date: date | None = None
    # Governance-assigned L1/L2 history. Most delegates have an empty list
    # (L3 candidates only, with daily eligibility computed at runtime).
    levels: list[LevelAssignment] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be non-empty")
        return v

    @model_validator(mode="after")
    def _end_date_after_start_date(self) -> "Delegate":
        if self.end_date is not None and self.end_date <= self.start_date:
            msg = f"end_date {self.end_date} must be after start_date {self.start_date} for delegate {self.name}"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _level_periods_fit_alignment(self) -> "Delegate":
        """Every LevelAssignment must fall within the delegate's alignment period.

        Returns:
            self

        Raises:
            ValueError: if any LevelAssignment falls outside the
            delegate's alignment period.
        """
        for la in self.levels:
            if la.start_date < self.start_date:
                msg = (
                    f"LevelAssignment star_dDate {la.start_date} for delegate "
                    f"{self.name} is before alignment start_date {self.start_date}"
                )
                raise ValueError(msg)
            if la.end_date is not None and self.end_date is not None and la.end_date > self.end_date:
                msg_0 = (
                    f"LevelAssignment end_date {la.end_date} for delegate "
                    f"{self.name} is after alignment end_date {self.end_date}"
                )
                raise ValueError(msg_0)
            # An open-ended LevelAssignment on an exited delegate is invalid.
            if la.end_date is None and self.end_date is not None:
                msg_1 = (
                    f"LevelAssignment for delegate {self.name} has no end_date but the delegate's alignment ends on "
                    f"{self.end_date}; set the LevelAssignment end_date too."
                )
                raise ValueError(msg_1)
        return self

    @model_validator(mode="after")
    def _level_periods_no_overlap(self) -> "Delegate":
        """Sequential LevelAssignments are allowed; overlapping ones aren't.

        Returns:
            self

        Raises:
            ValueError: if two LevelAssignments overlap, or a non-final
                one lacks an end_date.
        """
        if len(self.levels) <= 1:
            return self
        sorted_levels = sorted(self.levels, key=lambda la: la.start_date)
        for prev, curr in pairwise(sorted_levels):
            if prev.end_date is None:
                msg = (
                    f"LevelAssignment starting {prev.start_date} for delegate {self.name} has no end_date but is "
                    f"followed by another LevelAssignment starting {curr.start_date}; set the earlier end_date"
                )
                raise ValueError(msg)
            if prev.end_date >= curr.start_date:
                msg_0 = (
                    f"LevelAssignments for delegate {self.name} overlap period ending {prev.end_date} overlaps with "
                    f"period starting {curr.start_date}."
                )
                raise ValueError(msg_0)
        return self

    def is_active_during(self, period_start: date, period_end: date) -> bool:
        """True if this delegate was active at any point during the given period.

        end_date is inclusive; None means no upper bound.

        Returns:
            Whether the delegate's alignment overlaps the period.
        """
        if self.start_date > period_end:
            return False
        return not (self.end_date is not None and self.end_date < period_start)

    def level_at(self, d: date) -> int | None:
        """Return the governance level (1 or 2) on date d, or None if unassigned.

        Used by the L3 daily computation to determine whether a delegate
        is governance-assigned (and therefore not eligible for an L3 slot).
        """
        for la in self.levels:
            if la.covers(d):
                return la.level
        return None


class DelegatesConfig(BaseModel):
    """Top-level YAML structure: a list of delegates."""

    delegates: list[Delegate]

    @model_validator(mode="after")
    def _no_duplicate_addresses(self) -> "DelegatesConfig":
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

    Returns:
        Parsed and validated DelegatesConfig.

    Raises:
        ValueError: if YAML is empty.

    Notes:
        The function may raise FileNotFoundError, yaml.YAMLError, or
            pydantic.ValidationError from validation.
    """
    with Path(path).open(encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if raw is None:
        msg = f"{path} is empty or contains only YAML null"
        raise ValueError(msg)
    return DelegatesConfig.model_validate(raw)


def merge_with_api(
    yaml_config: DelegatesConfig,
    api_response: list[dict],
) -> tuple[list["Delegate"], list[str]]:
    """Verify the YAML roster against the API response.

    Drift rules:
    - YAML active, API absent -> warn (likely missed an exit update)
    - YAML exited, API absent -> expected, no warn
    - YAML exited, API present -> warn (date mismatch)
    - API present, not in YAML -> warn (new delegate missing from YAML)

    Comparisons are by vote delegate address (lowercased); names that
    differ but addresses match are not flagged.

    Returns:
        (canonical_roster, warnings). canonical_roster is the YAML's
        delegate list unchanged - the API never adds anyone.
    """
    warnings: list[str] = []

    yaml_by_address: dict[str, Delegate] = {d.vote_delegate_address.lower(): d for d in yaml_config.delegates}
    api_by_address: dict[str, dict] = {entry["voteDelegateAddress"].lower(): entry for entry in api_response}

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

    return list(yaml_config.delegates), warnings


@dataclass
class RosterResult:
    """Outcome of build_roster_for_period.

    Carries the active-delegate list plus metadata for the reconciliation log.
    """

    active_delegates: list[Delegate]
    drift_warnings: list[str]
    yaml_config: "DelegatesConfig"  # full config so callers can split active/exited
    api_delegate_count: int  # 0 if api_fetch_succeeded is False
    api_fetch_succeeded: bool


def build_roster_for_period(
    yaml_path: Path,
    period: MonthPeriod,
    api_fetcher: Callable[[], list[dict]],
    *,
    skip_api_check: bool = False,
) -> RosterResult:
    """Load YAML, optionally fetch API, run drift detection, filter to active-during-period.

    Drift detection compares the YAML against the live vote.sky.money
    listing. It's most useful at fetch time; finalize works on a
    closed historical period where renaming after the fact would be
    counterproductive, so finalize callers should pass
    skip_api_check=True.

    Returns:
        RosterResult with active delegates, drift warnings (empty when
        skip_api_check=True), the full YAML config, and API-fetch
        metadata.
    """
    yaml_config = load_delegates(yaml_path)

    api_response: list[dict] = []
    api_fetch_succeeded = False
    warnings: list[str] = []
    if skip_api_check:
        logger.info("API drift check skipped (skip_api_check=True).")
    else:
        try:
            api_response = api_fetcher()
            api_fetch_succeeded = True
            _, warnings = merge_with_api(yaml_config, api_response)
        except Exception as e:  # noqa: BLE001 — api_fetcher is caller-supplied; any failure degrades to YAML-only
            warnings = [
                (
                    f"API drift check skipped due to fetch failure: {type(e).__name__}: {e}. "
                    f"Proceeding with delegates.yaml as the sole source."
                )
            ]
            logger.warning("API fetch failed during drift check: %s", e)

    active = [d for d in yaml_config.delegates if d.is_active_during(period.start, period.end)]
    return RosterResult(
        active_delegates=active,
        drift_warnings=warnings,
        yaml_config=yaml_config,
        api_delegate_count=len(api_response),
        api_fetch_succeeded=api_fetch_succeeded,
    )


def to_dataframe(delegates: list["Delegate"]) -> pd.DataFrame:
    """Build the per-delegate Dataframe consumed by sky_protocol.

    Columns:'Delegate Name', 'Delegate Contract', 'Start Date'.
    Start Date is formatted '%Y-%m-%d - sky_protocol parses it back with
    date.fromisoformat, so the format matters.

    Returns:
        Three-column DataFrame, one row per delegate.
    """
    return pd.DataFrame({
        "Delegate Name": [d.name for d in delegates],
        "Delegate Contract": [d.vote_delegate_address for d in delegates],
        "Start Date": [d.start_date.strftime("%Y-%m-%d") for d in delegates],
    })
