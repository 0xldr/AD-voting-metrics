"""Pure functions for computing participation and communication metrics.

These metrics drive the Level 3 eligibility check (a candidate is ineligible on a given day if either participation or
communication is below 75%) and inform the Compensation tab's modifier column.

The functions take parsed status lists and return numeric results - no IO, no knowledge of the workbook structure.

## Status vocabulary

Each status is per-(poll, delegate). The same poll can have different statuses for different delegates depending on
alignment timing and SKY balance during the voting window:

  - "Yes": delegate had SKY delegated at some point during the voting window and cast a vote.
  - "No": delegate had SKY delegated at some point during the voting window but did not vote.
  - "Not Started": the delegate's alignment start_date is after this poll's end date - they weren't aligned yet when the
    poll closed, so non-participation isn't held against them.
  - "Exited": the delegate's alignment endDate is before this poll's start_date - they had already exited when the poll
    opened. Symmetric to "Not Started" on the other temporal boundary.
  - "Voting Open": the poll's voting window had not yet closed at the time metrics were computed. Goes in DISCOUNTED so
    non-voters aren't penalized until the poll closes and the data is re-run.
  - "No Delegated SKY": the delegate had zero SKY delegated to them on every day of the poll's voting window.
  - "Not included": operator-flagged poll to exclude (governance exception, data error) - set manually, not by the
    script.
  - "Pending verification": spell vote recorded but not yet verified; also used in Communication Master for cells
    awaiting operator review.
  - "Did not vote": derived value used in communication when participation = "No". Discounted from the communication
    percentage. Only appears in communication contexts.

## Participation Percentage

  participation_pct = participated / (participated + not_participated)

where "participated" counts statuses in PARTICIPATED ("Yes") and "not_participated" counts NOT_PARTICIPATED ("No"). All
other statuses are discounted from both numerator and denominator. Zero denominator returns None - callers map this to
"No Data".

The metric window is the last 6 months including the month being queried, keyed on poll start date. is_in_window accepts
an optional lower bound (None means unbounded back).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

# Spell-vote default for the (delegate participated) + (not yet verified)
# case. Shared across sheets, sky_executive, and sky_executive_onchain so
# the string only lives in one place.
PENDING_VERIFICATION = "Pending verification"

# Status sets. Frozensets for 0(1) membership and immutability - adding
# a new status (e.g. a new exclusion category) means updating one of these.
PARTICIPATED = frozenset({"Yes"})

NOT_PARTICIPATED = frozenset({"No"})

DISCOUNTED = frozenset({
    "Not Started",
    "Exited",
    "Voting Open",
    "No Delegated SKY",
    "Not included",
    PENDING_VERIFICATION,
    "Did not vote",
})


@dataclass(frozen=True)
class ParticipationCounts:
    """Breakdown of a percentage calculation, useful for debugging and audit."""

    participated: int
    not_participated: int
    discounted: int
    unknown: int  # unrecognized statuses; flagged for operator review.


def count_statuses(statuses: Iterable[str]) -> ParticipationCounts:
    """Bucket statuses into participated / not / discounted / unknown.

    A non-zero 'unknown' count means the workbook has a status the script doesn't know how to handle. Callers should
    flag it; silent fallthrough to discounted could mask a bug.

    Returns:
        ParticipationCounts with one count per bucket.
    """
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
    """Yes / (Yes + No), or None if the denominator is zero.

    None maps to the workbook's "No Data" sentinel - used for delegates with no participatable polls in the window.
    Unknown statuses are silently ignored; use count_statuses to detect them.

    Returns:
        Participation percentage as a float in [0.0, 1.0], or None.
    """
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
    """Return True if poll_start falls within [window_start, window_end] inclusive.

    window_start may be None for an unbounded-back window. Window membership is keyed on the poll's start date.

    Returns:
        True if the poll start date is in the window, False otherwise.
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
    """Participation percentage filtered to polls within the given window.

    poll_starts and statuses are parallel, one entry per poll; mismatched lengths surface through zip's strict mode.

    Returns:
        Participation percentage for in-window polls, or None.
    """
    in_window_statuses = [
        s for ps, s in zip(poll_starts, statuses, strict=True) if is_in_window(ps, window_start, window_end)
    ]
    return participation_pct(in_window_statuses)


def cross_reference_one(participation: str) -> str | None:
    """Return the implied communication value for a participation status, or None for passthrough.

    Encodes the rule that callers (sheets default-fill, metrics list-wise apply) share:

      - participation in NOT_PARTICIPATED → "Did not vote"
      - participation in DISCOUNTED → mirror the participation status
      - participation in PARTICIPATED or unknown → None (caller chooses passthrough or default)

    Returns:
        The override value, or None when the caller should keep its existing communication value.
    """
    if participation in NOT_PARTICIPATED:
        return "Did not vote"
    if participation in DISCOUNTED:
        return participation
    return None


def apply_participation_cross_reference(
    participation_statuses: Sequence[str],
    communication_statuses: Sequence[str],
) -> list[str]:
    """Apply the participation cross-reference rule to communication data.

    For each (poll, delegate) pair:

      - participation = "Yes" → communication passes through unchanged.
      - participation = "No" → communication becomes "Did not vote".
      - participation in DISCOUNTED -> communication mirrors the participation status (keeps both metric denominators in
        sync).
      - Unknown status → communication passes through (don't silently drop unrecognized statuses; count_statuses
        surfaces them).

    Mismatched sequence lengths surface through zip's strict mode.

    Returns:
        New list of cross-referenced communication statuses; inputs not mutated.
    """
    return [
        override if (override := cross_reference_one(p)) is not None else c
        for p, c in zip(participation_statuses, communication_statuses, strict=True)
    ]


def communication_pct(
    participation_statuses: Sequence[str],
    communication_statuses: Sequence[str],
) -> float | None:
    """Communication percentage: Yes / (Yes + No) on cross-referenced statuses.

    Applies apply_participation_cross_reference first (overriding "No" participation to "Did not vote"), then runs the
    same Yes/(Yes+No) calculation as participation_pct.

    The "communication must be within 7 days of poll end date" rule is followed by the operator at data entry - they
    only record "Yes" for in-window communications. The script trusts what's in the workbook.

    Returns:
        Communication percentage in [0.0, 1.0], or None for an empty denominator.
    """
    effective = apply_participation_cross_reference(participation_statuses, communication_statuses)
    return participation_pct(effective)


def communication_pct_for_window(
    poll_starts: Sequence[date],
    participation_statuses: Sequence[str],
    communication_statuses: Sequence[str],
    window_start: date | None,
    window_end: date,
) -> float | None:
    """Communication percentage filtered to polls within the given window.

    The three sequences are parallel, one entry per poll; mismatched lengths surface through zip's strict mode.

    Returns:
        Communication percentage for in-window polls, or None.
    """
    in_window = [
        (p, c)
        for ps, p, c in zip(poll_starts, participation_statuses, communication_statuses, strict=True)
        if is_in_window(ps, window_start, window_end)
    ]
    p_filtered = [p for p, _ in in_window]
    c_filtered = [c for _, c in in_window]
    return communication_pct(p_filtered, c_filtered)
