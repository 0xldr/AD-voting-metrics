"""Ballots - SKY governance polls and executive spells - and the participation statuses assigned to them.

Pure logic, no IO. A status is per-(delegate, ballot): the same ballot can carry different statuses for different
delegates depending on alignment timing and SKY balance during the voting window.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

import pandas as pd


@dataclass(frozen=True)
class Ballot:
    """A poll or executive spell, identified the way vote.sky.money identifies it.

    `id` is the poll id as a string, or the executive spell's contract address in lowercase. Polls close at
    POLL_CLOSE_TIME on `end`; spells have no end date, their voting deadline is derived from `start` (see
    `spell_vote_deadline`).
    """

    id: str
    start: date
    end: date | None
    title: str


# Participation status per (delegate contract, ballot id). Ballot ids are poll ids as strings or spell addresses.
type Statuses = dict[tuple[str, str], str]

# Delegate had SKY delegated at some point during the voting window and cast a vote.
YES = "Yes"

# Delegate had SKY delegated at some point during the voting window but did not vote.
NO = "No"

# Spell-only. Voted, but after the deadline (see spell_vote_deadline). Earns no credit, like NO, but labelled
# distinctly so the reader can tell "voted late" from "never voted".
LATE = "Late"

# The delegate's alignment start_date is after this ballot's end date, so non-participation isn't held against them.
NOT_STARTED = "Not Started"

# The delegate's alignment ended before this ballot opened, or during its window without a vote cast while still
# aligned, so non-participation isn't held against them.
EXITED = "Exited"

# The voting window had not yet closed when the data was fetched. Non-voters aren't penalized until it closes.
VOTING_OPEN = "Voting Open"

# The delegate had zero SKY delegated to them on every day of the voting window.
NO_DELEGATED_SKY = "No Delegated SKY"

# Spell-only. The delegate had SKY when the spell went live, but the on-chain check could not establish whether or
# when they voted.
PENDING_VERIFICATION = "Pending verification"

# Business days a delegate has to vote on an executive spell once it goes live.
SPELL_VOTE_BUSINESS_DAYS = 3

# SKY polls close at 16:00 UTC on their end date.
POLL_CLOSE_TIME = time(16, tzinfo=UTC)


def exited_before(end_date: date | None, day: date) -> bool:
    """True if a delegate's alignment (inclusive last day `end_date`) had ended before `day`."""
    return end_date is not None and end_date < day


def voted_while_aligned(vote_day: date | None, end_date: date | None) -> bool:
    """True if a vote was cast and, for a delegate who has since exited, no later than their inclusive last day."""
    return vote_day is not None and not exited_before(end_date, vote_day)


def spell_vote_deadline(spell_start: date) -> date:
    """Return the last UTC day on which a vote for a spell still counts.

    Counts SPELL_VOTE_BUSINESS_DAYS Monday-Friday days strictly after `spell_start`; no holiday calendar is applied.
    The returned date is inclusive. Live Monday gives Thursday; live Friday gives the following Wednesday. A weekend
    start counts from the Monday as day 1 (pandas' BDay rolls a weekend start back to Friday first).
    """
    return (pd.Timestamp(spell_start) + pd.offsets.BDay(SPELL_VOTE_BUSINESS_DAYS)).date()


def determine_vote_status(
    sky_by_date: dict[date, float],
    poll_end_date: date,
    *,
    delegate_voted: bool,
    current_datetime: datetime,
) -> str:
    """Determine the participation status for one (delegate, poll) pair.

    sky_by_date is the delegate's SKY balance per day across the voting window; missing dates count as zero.

    A vote is always YES. Otherwise, while the poll is still open the status is VOTING_OPEN, since the delegate may
    yet vote. Once closed, a non-voter is NO only if they held SKY both on the close day and on at least one earlier
    day of the window; without stake at close they can't be held responsible, so the status is NO_DELEGATED_SKY.
    """
    if delegate_voted:
        return YES

    if current_datetime < datetime.combine(poll_end_date, POLL_CLOSE_TIME):
        return VOTING_OPEN

    sky_at_close = sky_by_date.get(poll_end_date, 0.0)
    sky_before_close = any(sky != 0 for d, sky in sky_by_date.items() if d < poll_end_date)
    return NO if sky_at_close and sky_before_close else NO_DELEGATED_SKY
