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
  - "Voting Open": the poll's voting window had not yet closed at the time metrics were
  computed. Delegates who already voted are "Yes"; non-voters get this status
  (rather than "No") because the result is still in flux and not voting yet isn't a
  final answer. Goes in DISCOUNTED so it doesn't affect participation % until the poll
  closes and the data is rerun.
  - "No Delegated SKY": the delegate had zero SKY delegated to them on every day
  of the poll's voting window
  - "Not included": operator-flagged poll to exclude (governance exception,
    data error, etc.) — set manually, not by the script
  - "Pending verification": spell vote recorded but not yet verified
  (spell-specific status that requires manual confirmation); also used
  by the Communication Master tab to mark cells that have not yet been reviewed
  by the operator
  - "Did Not Vote": derived value for communication metrics, used when a delegate did
  not vote on a poll (participation = "No"). There is nothing to communicate about on
  a poll that a delegate did not vote on, so the poll is excluded from the communication
  percentage.

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
        "Voting Open",
        "No Delegated SKY",
        "Not included",
        "Pending verification",
        "Did not vote",
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


def apply_participation_cross_reference(
    participation_statuses: Sequence[str],
    communication_statuses: Sequence[str],
) -> list[str]:
    """Apply the participation cross-reference rule to communication data.

    For each (poll, delegate) pair:

      - participation = "Yes" → communication passes through unchanged.
        The delegate participated; whatever the operator recorded is the
        truth.
      - participation = "No" → communication is overridden to "Did not
        vote". There's nothing to communicate about a poll the delegate
        didn't engage with; the discount keeps it out of the comm
        denominator.
      - participation in DISCOUNTED (Not Started, Exited, Voting Open,
        No Delegated SKY, Not included, Pending verification) →
        communication is overridden to that same participation status.
        Polls outside the delegate's alignment period or otherwise
        ineligible for participation can't meaningfully contribute to a
        communication metric either. Mirroring the participation status
        into communication keeps both denominators in sync.
      - participation = anything else (unknown status) → communication
        passes through. Don't silently lose data on unrecognized
        statuses; let count_statuses flag them later.

    The two sequences must be parallel and equal-length. Mismatched
    lengths raise ValueError.

    Returns a new list (does not mutate inputs).
    """
    if len(participation_statuses) != len(communication_statuses):
        raise ValueError(
            f"participation_statuses and communication_statuses must be the "
            f"same length (got {len(participation_statuses)}) and "
            f"{len(communication_statuses)}"
        )
    result = []
    for p, c in zip(participation_statuses, communication_statuses, strict=True):
        if p in NOT_PARTICIPATED:
            result.append("Did not vote")
        elif p in DISCOUNTED:
            # Mirror the discounted participation
            result.append(p)
        else:
            # "Yes" or any unknown status
            result.append(c)

    return result


def communication_pct(
    participation_statuses: Sequence[str],
    communication_statuses: Sequence[str],
) -> float | None:
    """Communication percentage: Yes / (Yes + No) on the cross-referenced
    communication status list.

    Applies apply_participation_cross_reference first (overriding "No"
    participation to "Did not vote" communication), then runs the same
    Yes/(Yes+No) calculation as participation_pct. Returns None for an
    empty denominator, matching the "No Data" sentinel pattern.

    The two sequences must be parallel and equal-length. Length mismatch
    raises ValueError via apply_participation_cross_reference.

    Note: this function does not enforce the "communication must be within
    7 days of poll end date" rule. That rule is followed by the operator at
    data entry time — operators only record "Yes" for communications that
    happened in the window. The script trusts what's in the workbook.
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

    poll_starts, participation_statuses, and communication_statuses must
    be three parallel sequences of equal length, one entry per poll.
    Window membership uses the same is_in_window check as participation
    (keyed on poll start date).

    Mismatched lengths raise ValueError.
    """
    n = len(poll_starts)
    if len(participation_statuses) != n or len(communication_statuses) != n:
        raise ValueError(
            f"poll_starts, participation_statuses, and communication_statuses "
            f"must all be the same length (got {n}, "
            f"{len(participation_statuses)}, {len(communication_statuses)})"
        )
    in_window_indices = [
        i for i, ps in enumerate(poll_starts) if is_in_window(ps, window_start, window_end)
    ]
    p_filtered = [participation_statuses[i] for i in in_window_indices]
    c_filtered = [communication_statuses[i] for i in in_window_indices]
    return communication_pct(p_filtered, c_filtered)
