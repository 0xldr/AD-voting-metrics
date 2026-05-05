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

Drift detection (handled in the roster module, not here): every entry
with endDate=None should appear in the API's currently-aligned response;
every API-returned aligned delegate should have a matching entry with
endDate=None. Mismatches are warnings — typically signalling that the
YAML needs updating after a new alignment or an exit.
"""

from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


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
