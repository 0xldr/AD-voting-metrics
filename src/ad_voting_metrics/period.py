"""Domain type for a single calendar month."""

import calendar
import time
from dataclasses import dataclass
from datetime import date

YEAR_LOWER_BOUND = 2022

# Accepted --month spellings: "April 2026", "Apr 2026", "2026-04". Month names match case-insensitively.
_MONTH_FORMATS = ("%B %Y", "%b %Y", "%Y-%m")


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

        Accepts the spellings in _MONTH_FORMATS; surrounding whitespace is ignored.

        Returns:
            A MonthPeriod for the parsed (year, month).

        Raises:
            ValueError: if the input matches none of the accepted formats.
        """
        for fmt in _MONTH_FORMATS:
            try:
                parsed = time.strptime(value.strip(), fmt)
            except ValueError:
                continue
            return cls(year=parsed.tm_year, month=parsed.tm_mon)
        msg = f"could not parse {value!r} as a month. Try formats like 'April 2026' or '2026-04'."
        raise ValueError(msg)
