"""Roster of aligned delegates, loaded from delegates.yaml.

The YAML is the source of truth for who is or has been an Aligned Delegate.
Each entry has:

- name: the delegate's display name (matches what the spreadsheet uses)
- voteDelegateAddress: the on-chain vote delegate contract (lowercase 0x...)
- startDate: the date AD compensation begins. Note this is NOT necessarily
  the contract creation date returned by the vote.sky.money API as
  `creationDate` — a delegate may deploy their contract weeks before
  formally being aligned.
- endDate: optional. If set, the inclusive last day they were an AD;
  endDate of 2026-04-15 means they were active on April 15.

Drift detection: every YAML entry with endDate=None should appear in the API's
currently-aligned response; every API-returned AD should have a matching entry
with endDate=None. Mismatches produce warnings - typically that the YAML needs
updating after a new alignment or an exit.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .period import MonthPeriod

logger = logging.getLogger(__name__)


class Delegate(BaseModel):
    """A single AD entry, currently or previously active."""

    name: str
    voteDelegateAddress: str = Field(pattern=r"^0x[0-9a-f]{40}$")
    startDate: date
    endDate: date | None = None

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("name must be non-empty")
        return v

    @model_validator(mode="after")
    def _end_date_after_start_date(self) -> "Delegate":
        if self.endDate is not None and self.endDate <= self.startDate:
            raise ValueError(
                f"endDate {self.endDate} must be after startDate {self.startDate} "
                f"for delegate {self.name}"
            )
        return self

    def is_active_during(self, period_start: date, period_end: date) -> bool:
        """True if this delegate was active at any point during the given period.

        endDate is inclusive. Delegates with no endDate have no upper bound.
        """
        if self.startDate > period_end:
            return False
        return not (self.endDate is not None and self.endDate < period_start)


class DelegatesConfig(BaseModel):
    """Top-level YAML structure: a list of delegates."""

    delegates: list[Delegate]

    @model_validator(mode="after")
    def _no_duplicate_addresses(self) -> "DelegatesConfig":
        seen: dict[str, str] = {}
        for d in self.delegates:
            if d.voteDelegateAddress in seen:
                raise ValueError(
                    f"Duplicate voteDelegateAddress {d.voteDelegateAddress} for "
                    f"{d.name} and {seen[d.voteDelegateAddress]}"
                )
            seen[d.voteDelegateAddress] = d.name
        return self


def load_delegates(path: Path) -> DelegatesConfig:
    """Load and validate the delegates YAML from the given path.

    Raises FileNotFoundError if the file doesn't exist, yaml.YAMLError if is't malformed,
    and pydantic.ValidationError on schema violations.
    """
    with Path(path).open() as f:
        raw = yaml.safe_load(f)
    if raw is None:
        raise ValueError(f"{path} is empty or contains only YAML null")
    return DelegatesConfig.model_validate(raw)


def merge_with_api(
    yaml_config: DelegatesConfig,
    api_response: list[dict],
) -> tuple[list["Delegate"], list[str]]:
    """Verify the YAML roster against the API response.

    Returns the YAML's delegates as the canonical roster (the API
    does not add anyone - the YAML is source of truth) plus a list
    of human-readable warning strings for any drift detected.

    Drift rules:
    - YAML active (endDate=None), API absent -> warn (operator likely
    forgot to set endDate after an exit)
    - YAML exited (endDate set), API absent -> expected, no warn
    - YAML exited, API present -> warn (date mismatch; either YAML
    or API is wrong about the exit)
    - API present, not in YAML -> warn (new aligned delegate not yet
    added to YAML)

    Comparisons are by voteDelegateAddress (lowercased). Names that differ
    but addresses match are not flagged.
    """
    warnings: list[str] = []

    yaml_by_address: dict[str, Delegate] = {
        d.voteDelegateAddress.lower(): d for d in yaml_config.delegates
    }
    api_by_address: dict[str, dict] = {
        entry["voteDelegateAddress"].lower(): entry for entry in api_response
    }

    for addr, delegate in yaml_by_address.items():
        in_api = addr in api_by_address
        if delegate.endDate is None and not in_api:
            warnings.append(
                f"{delegate.name} ({addr}) is marked active in YAML "
                f"(endDate=null) but does not appear in the API as currently "
                f"aligned. Did they exit? Update endDate in delegates.yaml"
            )
        elif delegate.endDate is not None and in_api:
            warnings.append(
                f"{delegate.name} ({addr}) is marked exited in YAML "
                f"(endDate={delegate.endDate}) but the API still shows them "
                f"as currently aligned. Date mismatch - verify which is correct."
            )

    for addr in api_by_address:
        if addr not in yaml_by_address:
            api_name = api_by_address[addr].get("name", "?")
            warnings.append(
                f"{api_name} ({addr}) appears in the API as currently "
                "aligned but is not in delegates.yaml. Add an entry."
            )

    return list(yaml_config.delegates), warnings


@dataclass
class RosterResult:
    """Outcome of build_roster_for_period.

    Carries the active-delegates list (the runtime-relevant output) plus
    counts and metadata that downstream callers (the reconciliation log,
    in particular) need to record what happened during roster building.
    """

    active_delegates: list[Delegate]
    drift_warnings: list[str]
    yaml_config: "DelegatesConfig"  # full config so reconciliation can split active/exited
    api_delegate_count: int  # 0 if api_fetch_succeeded is False
    api_fetch_succeeded: bool


def build_roster_for_period(
    yaml_path: Path,
    period: MonthPeriod,
    api_fetcher: Callable[[], list[dict]],
) -> RosterResult:
    """Load YAML, fetch API, run drift detection, filter to active-during-period.

    The api_fetcher is a callable that returns the raw API delegate list.

    If the API fetch raises, drift detection is skipped and the script proceeds
    with YAML alone. This is a deliberate soft-fail, YAML is the source of truth.

    Returns a RosterResult with the active delegates, drift warnings, the full
    YAML config, and metadata about API counts and fetch success — for the
    reconciliation log.
    """
    yaml_config = load_delegates(yaml_path)

    api_response: list[dict] = []
    api_fetch_succeeded = False
    try:
        api_response = api_fetcher()
        api_fetch_succeeded = True
        _, warnings = merge_with_api(yaml_config, api_response)
    except Exception as e:
        warnings = [
            f"API drift check skipped due to fetch failure: {type(e).__name__}: {e}. "
            f"Proceeding with delegates.yaml as the sole source."
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
    """Build a pandas DataFrame in the shape of the sky_dao functions expect.

    sky_dao reads three colums:
    - 'Delegate Name': (str)
    - 'Delegate Contract': (str, lowercase 0x...address)
    - 'Start Date': (str, formatted '%Y-%m-%d - sky_dao parses it back
    with strptime, so format matters)
    """
    return pd.DataFrame(
        {
            "Delegate Name": [d.name for d in delegates],
            "Delegate Contract": [d.voteDelegateAddress for d in delegates],
            "Start Date": [d.startDate.strftime("%Y-%m-%d") for d in delegates],
        }
    )
