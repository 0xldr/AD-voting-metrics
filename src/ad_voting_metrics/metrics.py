"""Pure functions for computing participation and communication metrics.

These metrics drive the Level 3 eligibility check (a candidate is ineligible
on a given day if either participation or communication is below 75%) and
inform the Compensation tab's modifier column.

Status vocabulary matches what the script writes (or will write) into the
Participation Raw Data tab. Each status is per-(poll, delegate) - the
same poll can have different statuses for different delegates depending on
their alignment timing and SKY balance during the voting window:

  - "Yes": delegate had SKY delegated at some point during the voting window
   and cast a vote
  - "No": delegate had SKY delegated at some point during the voting window
   but did not vote
  - "Not Started": the delegate's alignment startDate is after the poll's end
  -i.e., they weren't aligned yet when the poll closed, so non-participation
  isn't held against them
  - "Exited": the delegate's alignment endDate is before this poll's startDate -i.e.,
  they had already exited when the poll opened, so non-participation isn't held against
  them. Symmetric to "Not Started" on the other temporal boundary.
  - "No Delegated SKY": the delegate had zero SKY delegated to them on every day
  of the poll's voting window
  - "Not included": operator-flagged poll to exclude (governance exception,
    data error, etc.) — set manually, not by the script
  - "Pending verification": spell vote recorded but not yet verified

The participation percentage matches the existing formula:

  participation_pct = participated / (participated + not_participated)

where "participated" counts statuses in PARTICIPATED (just "Yes") and
"not_participated" counts NOT_PARTICIPATED ("No"). All other statuses are
discounted from both numerator and denominator. If the denominator is zero
(no polls were votable in the window), the function returns None — callers
typically map this to the workbook's "No Data" sentinel.

The metric window is the last 6 months including the month being queried,
keyed on poll start date. is_in_window remains generic over the lower bound
(accepting None) so future metric variants can plug in without reshaping the
API.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

PARTICIPATED = frozenset({"Yes"})

NOT_PARTICIPATED = frozenset({"No"})

DISCOUNTED = frozenset(
    {
        "Not Started",
        "Exited",
        "No Delegated SKY",
        "Not included",
        "Pending verification",
    }
)


@dataclass(frozen=True)
class ParticipationCounts:
    """Breakdown of a percentage calculation, useful for debugging and audit."""

    participated: int
    not_participated: int
    discounted: int
    unknown: int


def count_statuses(statuses: Iterable[str]) -> ParticipationCounts:
    """Bucket a list of statuses into participated / not / discounted / unknown."""
    p = np = d = u = 0
    for s in statuses:
        if s in PARTICIPATED:
            p += 1
        elif s in NOT_PARTICIPATED:
            np += 1
        elif s in DISCOUNTED:
            d += 1
        else:
            u += 1
    return ParticipationCounts(participated=p, not_participated=np, discounted=d, unknown=u)


def participation_pct(statuses: Iterable[str]) -> float | None:
    """Yes / (Yes + No), or None if the denominator is zero."""
    counts = count_statuses(statuses)
    denominator = counts.participated + counts.not_participated
    if denominator == 0:
        return None
    return counts.participated / denominator


def is_in_window(
    poll_start: date,
    window_start: date | None,
    window_end: date,
) -> bool:
    """True if poll_start falls within [window_start, window_end] inclusive.

    window_start may be None to denote an unbounded-back window. Production
    callers always pass a concrete date (six months before window_end), but
    the API leaves the lower bound optional so future metric variants don't
    require API reshaping.
    """
    if poll_start > window_end:
        return False
    return not (window_start is not None and poll_start < window_start)


def participation_pct_for_window(
    poll_starts: Sequence[date],
    statuses: Sequence[str],
    window_start: date | None,
    window_end: date,
) -> float | None:
    """Participation percentage filtered to polls within the given window."""
    if len(poll_starts) != len(statuses):
        raise ValueError(
            f"poll_starts and statuses must be the same length "
            f"got {len(poll_starts)} and {len(statuses)}"
        )
    in_window_statuses = [
        s
        for ps, s in zip(poll_starts, statuses, strict=True)
        if is_in_window(ps, window_start, window_end)
    ]
    return participation_pct(in_window_statuses)
