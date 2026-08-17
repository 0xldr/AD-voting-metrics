"""Domain type for a single calendar month."""

import calendar
from dataclasses import dataclass
from datetime import date

import pandas as pd

YEAR_LOWER_BOUND = 2022


@dataclass(frozen=True)
class MonthPeriod:
    """A single calendar month, identified by year and 1-indexed month number.

    Use the .start and .end properties to get date bounds for the month. Construct from a string like "April 2026" via
    MonthPeriod.from_string().
    """

    year: int
    month: int

    def __post_init__(self) -> None:
        """Reject out-of-range months and pre-2022 years.

        Raises:
            ValueError: if month is outside 1..12 or year is before the project's lower bound.
        """
        if not 1 <= self.month <= 12:  # noqa: PLR2004 — calendar bounds
            msg = f"month must be in 1..12, got {self.month}"
            raise ValueError(msg)
        if self.year < YEAR_LOWER_BOUND:
            msg = f"year must be >= {YEAR_LOWER_BOUND}, got {self.year}"
            raise ValueError(msg)

    @property
    def start(self) -> date:
        """First day of the month."""
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        """Last day of the month, accounting for variable month length."""
        last_day = calendar.monthrange(self.year, self.month)[1]
        return date(self.year, self.month, last_day)

    def __str__(self) -> str:
        """Render as April 2026 rather than "MonthPeriod(year=2026, month=4)".

        Returns:
            The human-readable form.
        """
        return date(self.year, self.month, 1).strftime("%B %Y")

    @classmethod
    def from_string(cls, value: str) -> MonthPeriod:
        """Parse a human or ISO month string into a MonthPeriod.

        Accepts "April 2026", "Apr 2026", "2026-04", and similar formats. Day component is ignored; only year and month
        are read.

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
