"""Close-day vote-status rule for SKY polls.

Pure logic: given a delegate's SKY balance across a poll's voting window and whether they voted, decide the
participation status string.
"""

from datetime import UTC, date, datetime, time


def determine_vote_status(
    sky_by_date: dict[date, float],
    poll_end_date: date,
    *,
    delegate_voted: bool,
    current_datetime: datetime,
) -> str:
    """Determine the participation status for one (delegate, poll) pair.

    sky_by_date is the delegate's SKY balance per day across the voting window. Missing dates are treated as zero.
    poll_end_date is the poll's close day; voting on SKY polls ends at 16:00 UTC.

    Rule:

    - If the poll is still open (current_datetime < 16:00 UTC on poll_end_date), the result is still in flux:
        - Voted -> "Yes"
        - Not voted -> "Voting Open" (DISCOUNTED, doesn't penalize) A non-voter might still vote before close, so
          marking them "No" now would be wrong; re-running after close resolves the status.

    - If the poll has closed, apply the close-day rule. A delegate is on the hook for "No" only if BOTH hold:

        1. Non-zero SKY on the close day, AND
        2. Non-zero SKY on at least one prior day in the window.

      Otherwise the status is "No Delegated SKY":

        - Zero throughout window           -> No Delegated SKY
        - Had SKY at some point AND voted  -> Yes
        - SKY before AND at close, no vote -> No

    Without stake at close, a delegate can't be held responsible for not voting.

    Returns:
        One of "Yes", "No", "No Delegated SKY", or "Voting Open".
    """
    poll_close_at = datetime.combine(poll_end_date, time(16, tzinfo=UTC))

    if current_datetime < poll_close_at:
        return "Yes" if delegate_voted else "Voting Open"

    if delegate_voted:
        return "Yes"

    close_day_sky = sky_by_date.get(poll_end_date, 0.0)
    had_sky_before_close = any(sky != 0 for d, sky in sky_by_date.items() if d < poll_end_date)

    if close_day_sky == 0 or not had_sky_before_close:
        return "No Delegated SKY"

    return "No"
