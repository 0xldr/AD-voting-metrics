"""Domain type for a single calendar month."""

from dataclasses import dataclass
from datetime import date

import pandas as pd

YEAR_LOWER_BOUND = 2022


@dataclass(frozen=True)
class MonthPeriod:
    """A single calendar month, identified by year and 1-indexed month number.

    Use the .start and .end properties to get date bounds for the month.
    Construct from a string like "April 2026" via MonthPeriod.from_string().
    """

    year: int
    month: int

    def __post_init__(self) -> None:
        """Normalize out-of-range months into adjacent years; reject pre-2022 years.

        Constructing with month outside 1..12 rolls into the neighboring year
        (e.g. month=13 becomes the next January, month=0 becomes the previous
        December).

        Raises:
            ValueError: if the (normalized) year is before the project's lower bound.
        """
        normalized = pd.Period(year=self.year, month=self.month, freq="M")
        if (normalized.year, normalized.month) != (self.year, self.month):
            object.__setattr__(self, "year", normalized.year)
            object.__setattr__(self, "month", normalized.month)
        if self.year < YEAR_LOWER_BOUND:
            msg = f"year must be >= {YEAR_LOWER_BOUND}, got {self.year}"
            raise ValueError(msg)

    @property
    def _period(self) -> pd.Period:
        return pd.Period(year=self.year, month=self.month, freq="M")

    @property
    def start(self) -> date:
        """First day of the month."""
        return self._period.start_time.date()

    @property
    def end(self) -> date:
        """Last day of the month, accounting for variable month length."""
        return self._period.end_time.date()

    def __str__(self) -> str:
        """Render as April 2026 rather than "MonthPeriod(year=2026, month=4)".

        Returns:
            The human-readable form.
        """
        return self._period.strftime("%B %Y")

    @classmethod
    def from_string(cls, value: str) -> "MonthPeriod":
        """Parse a human or ISO month string into a MonthPeriod.

        Accepts "April 2026", "Apr 2026", "2026-04", and similar formats.
        Day component is ignored; only year and month are read.

        Returns:
            A MonthPeriod for the parsed (year, month).

        Raises:
            ValueError: if the input cannot be parsed as a month.
        """
        msg = f"could not parse {value!r} as a month. Try formats like 'April 2026' or '2026-04'."
        try:
            p = pd.Period(value, freq="M")
        except (ValueError, TypeError) as e:
            raise ValueError(msg) from e
        # pd.Period("") returns NaT instead of raising; pandas-stubs doesn't model that.
        if p is pd.NaT:  # type: ignore[comparison-overlap]
            raise ValueError(msg)
        return cls(year=p.year, month=p.month)
