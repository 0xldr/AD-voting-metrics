"""Domain type for a single calendar month.

Carrying the month identity (year + month) explicitly rather than a
(start_date, end_date) tuple keeps the month name available without
having to derive it back from dates and catches a class of bugs where
two date arguments get swapped.
"""

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime

from dateutil import parser as dateparser

YEAR_LOWER_BOUND = 2022
MONTHS_IN_YEAR = 12


@dataclass(frozen=True)
class MonthPeriod:
    """A single calendar month, identified by year and 1-indexed month number.

    Use the .start and .end properties to get date bounds for the month.
    Construct from a string like "April 2026" via MonthPeriod.from_string().
    """

    year: int
    month: int

    def __post_init__(self) -> None:
        """Validate month is 1..12 and year is >=2022.

        Raises:
            ValueError: if month is out of range or year is before 2022.
        """
        if not 1 <= self.month <= MONTHS_IN_YEAR:
            msg = f"month must be 1..12, got {self.month}"
            raise ValueError(msg)
        # No upper bound on year — the CLI rejects future months elsewhere.
        if self.year < YEAR_LOWER_BOUND:
            msg_0 = f"year must be >= 2022, got {self.year}"
            raise ValueError(msg_0)

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
        return f"{calendar.month_name[self.month]} {self.year}"

    @classmethod
    def from_string(cls, value: str) -> "MonthPeriod":
        """Parse a human or ISO month string into a MonthPeriod.

        Accepts "April 2026", "Apr 2026", "2026-04", and other formats
        dateutil.parser handles. Day component is ignored; only year
        and month are read. Future months are NOT rejected - that's a
        CLI-input concern.

        Returns:
            A MonthPeriod for the parsed (year, month)

        Raises:
            ValueError: if the input cannot be parsed as a month.
        """
        # Lazy-imported to keep dateutil off the import path for callers
        # that construct MonthPeriod directly from year/month.

        # Fixed sentinel default so dateutil doesn't fill the day from
        # today's date - "April 2026" on May 5 would otherwise be April 5.
        sentinel = datetime(2000, 1, 1, tzinfo=UTC)
        try:
            parsed = dateparser.parse(value, default=sentinel)
        except (ValueError, TypeError, OverflowError) as e:
            msg = f"could not parse {value!r} as a month. Try formats like 'April 2026' or '2026-04'."
            raise ValueError(msg) from e

        return cls(year=parsed.year, month=parsed.month)
