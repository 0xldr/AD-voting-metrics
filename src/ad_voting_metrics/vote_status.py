"""Participation-status vocabulary and the rules that assign it, for SKY polls and executive spells.

Pure logic, no IO. A status is per-(delegate, poll): the same poll can carry different statuses for different delegates
depending on alignment timing and SKY balance during the voting window.

  - `determine_vote_status`: given a delegate's SKY balance across a poll's voting window and whether they voted,
    decide the poll status.
  - `spell_vote_deadline`: the last day a vote for an executive spell still counts.

Spell statuses are seeded in `sources.sky_executive` from balances and alignment dates, then settled on-chain in
`sources.sky_executive_onchain`.
"""

from datetime import UTC, date, datetime, time, timedelta

# ---------------------------------------------------------------------------
# Status vocabulary
# ---------------------------------------------------------------------------

# Delegate had SKY delegated at some point during the voting window and cast a vote.
YES = "Yes"

# Delegate had SKY delegated at some point during the voting window but did not vote.
NO = "No"

# Spell-only. The delegate voted for the executive, but after the 3-business-day
# deadline (see spell_vote_deadline). Counted as non-participation - a vote past
# the deadline earns no credit - but labelled distinctly from NO so the reader
# can tell "voted late" from "never voted".
LATE = "Late"

# The delegate's alignment start_date is after this poll's end date: they weren't
# aligned yet when the poll closed, so non-participation isn't held against them.
NOT_STARTED = "Not Started"

# The poll's voting window had not yet closed when the data was fetched.
# Non-voters aren't penalized until the poll closes and the data is re-run.
VOTING_OPEN = "Voting Open"

# The delegate had zero SKY delegated to them on every day of the voting window.
NO_DELEGATED_SKY = "No Delegated SKY"

# Spell-only. The delegate had SKY when the spell went live, but the on-chain
# check could not establish whether or when they voted.
PENDING_VERIFICATION = "Pending verification"


# ---------------------------------------------------------------------------
# Executive spell deadline
# ---------------------------------------------------------------------------

# Business days a delegate has to vote on an executive spell once it goes live.
SPELL_VOTE_BUSINESS_DAYS = 3

# Monday-Friday. date.weekday() returns 0 for Monday, so 5 and 6 are the weekend.
_SATURDAY = 5


def spell_vote_deadline(spell_start: date, business_days: int = SPELL_VOTE_BUSINESS_DAYS) -> date:
    """Return the last UTC day on which a vote for a spell still counts.

    Counts `business_days` Monday-Friday days strictly after `spell_start`, skipping weekends; no holiday calendar is
    applied. The returned date is inclusive - a vote landing anywhere within it (up to 23:59:59 UTC) is on time.

    Live Monday gives a Thursday deadline; live Friday gives the following Wednesday. A spell going live on a weekend
    starts counting from the Monday, which is day 1.

    Returns:
        The inclusive deadline date.
    """
    deadline = spell_start
    remaining = business_days
    while remaining > 0:
        deadline += timedelta(days=1)
        if deadline.weekday() < _SATURDAY:
            remaining -= 1
    return deadline


# ---------------------------------------------------------------------------
# Poll close-day rule
# ---------------------------------------------------------------------------


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
        - Voted -> YES
        - Not voted -> VOTING_OPEN. A non-voter might still vote before close, so marking them NO now would be
          wrong; re-running after close resolves the status.

    - If the poll has closed, apply the close-day rule. A delegate is on the hook for NO only if BOTH hold:

        1. Non-zero SKY on the close day, AND
        2. Non-zero SKY on at least one prior day in the window.

      Otherwise the status is NO_DELEGATED_SKY:

        - Zero throughout window           -> No Delegated SKY
        - Had SKY at some point AND voted  -> Yes
        - SKY before AND at close, no vote -> No

    Without stake at close, a delegate can't be held responsible for not voting.

    Returns:
        One of YES, NO, NO_DELEGATED_SKY, or VOTING_OPEN.
    """
    poll_close_at = datetime.combine(poll_end_date, time(16, tzinfo=UTC))

    if current_datetime < poll_close_at:
        return YES if delegate_voted else VOTING_OPEN

    if delegate_voted:
        return YES

    close_day_sky = sky_by_date.get(poll_end_date, 0.0)
    had_sky_before_close = any(sky != 0 for d, sky in sky_by_date.items() if d < poll_end_date)

    if close_day_sky == 0 or not had_sky_before_close:
        return NO_DELEGATED_SKY

    return NO
