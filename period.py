"""Domain type for "a single calendar month."

The script processes one month at a time. Carrying the month identity
(year + month) explicitly is more useful than carrying a (start_date, end_date)
tuple because:

- Future code (Sheets writer, reconciliation log) keys data by month name,
  not by date range. A MonthPeriod naturally provides that key.
- The (start, end) view is derivable from (year, month) but not vice versa
  without reasoning about the dates — a tuple of (April 1, April 30) doesn't
  intrinsically say "April 2026."
- It catches a class of bugs where two date arguments get swapped.
"""

import calendar
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class MonthPeriod:
    """A single calendar month, identified by year and 1-indexed month number.

    Use the .start and .end properties to get the date bounds for the month.
    Construct from a string like "April 2026" via MonthPeriod.from_string().
    """

    year: int
    month: int

    def _post_init__(self):
        if not 1 <= self.month <= 12:
            raise ValueError(f"month must be in 1..12, got {self.month}")
        # No upper bound on year
        if self.year < 1900:
            raise ValueError(f"year must be >= 1900, got {self.year}")

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
        return f"{calendar.month_name[self.month]} {self.year}"

    @classmethod
    def from_string(cls, value: str) -> "MonthPeriod":
        """Parse a human or ISO-style month string into a MonthPeriod.

        Accepts "April 2026", "Apr 2026", "2026-04", and other formats
        dateutil.parser can interpret. The day component (if any) is ignored;
        only year and month are read.

        Raises ValueError on unparseable input. Does NOT reject future months —
        that's a CLI-input concern, handled by the argparse type callback.
        """
        # Imported here rather than at module top to keep the dependency
        # local to the parsing path. Other paths (constructing MonthPeriod
        # directly from year/month) don't need dateutil.
        from datetime import datetime

        from dateutil import parser as dateparser

        # dateutil fills unspecified components from the default. Without an
        # explicit default it uses today's date, so on May 5 the string
        # "April 2026" parses as April 5, 2026 (with today's day=5). Use a
        # fixed sentinel so only year and month are trusted.
        sentinel = datetime(2000, 1, 1)
        try:
            parsed = dateparser.parse(value, default=sentinel)
        except (ValueError, TypeError, OverflowError) as e:
            raise ValueError(
                f"could not parse {value!r} as a month. Try formats like 'April 2026' or '2026-04'."
            ) from e

        return cls(year=parsed.year, month=parsed.month)
