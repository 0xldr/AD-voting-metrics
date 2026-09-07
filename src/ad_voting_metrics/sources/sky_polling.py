"""vote.sky.money polling endpoints: poll listing + per-poll voter tallies."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any

import pandas as pd

from ad_voting_metrics.ballots import (
    EXITED,
    NOT_STARTED,
    Ballot,
    Statuses,
    determine_vote_status,
    exited_before,
    voted_while_aligned,
)
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate

from .chain import utc_date
from .http import fetch_json, paginate

logger = logging.getLogger(__name__)

SKY_ALL_POLLS_URL = "https://vote.sky.money/api/polling/all-polls"
SKY_POLL_ID_URL = "https://vote.sky.money/api/polling/tally"

# The API's default page size for the all-polls endpoint.
SKY_POLL_PAGE_SIZE = 30

# Max concurrent voter-list fetches in poll_statuses. vote.sky.money
# tolerates this comfortably; raising it gives diminishing returns.
_POLL_VOTER_FETCH_CONCURRENCY = 8


def fetch_polls_for_period(period: MonthPeriod) -> list[Ballot]:
    """Fetch polls from vote.sky.money that started within the period, as Ballots.

    The request's startDate parameter sets the lower bound and the listing comes back oldest-first, so paging stops at
    the first poll that starts after the period.
    """

    def polls_page(number: int) -> list[dict[str, Any]]:
        data = fetch_json(
            SKY_ALL_POLLS_URL,
            network="mainnet",
            pageSize=SKY_POLL_PAGE_SIZE,
            page=number,
            orderBy="FURTHEST_START",
            startDate=period.start.isoformat(),
        )
        return list(data.get("polls", []))

    polls: list[Ballot] = []
    for page in paginate(polls_page):
        for poll in page:
            start = datetime.fromisoformat(poll["startDate"]).date()
            if start > period.end:
                logger.info("Fetched %d polls starting in %s", len(polls), period)
                return polls
            if start >= period.start:
                polls.append(
                    Ballot(
                        id=str(poll["pollId"]),
                        start=start,
                        end=datetime.fromisoformat(poll["endDate"]).date(),
                        title=poll["title"],
                    )
                )

    logger.info("Fetched %d polls starting in %s", len(polls), period)
    return polls


def _fetch_poll_votes(poll: Ballot) -> tuple[str, dict[str, date]]:
    """Fetch one poll's votes as a mapping of lowercased voter address to the UTC day the vote was recorded.

    Returns a tuple of (poll id, votes by voter).
    """
    data = fetch_json(f"{SKY_POLL_ID_URL}/{poll.id}", network="mainnet")
    votes = {vote["voter"].lower(): utc_date(int(vote["blockTimestamp"])) for vote in data.get("votesByAddress", [])}
    return poll.id, votes


def poll_statuses(
    polls: list[Ballot],
    delegates: list[Delegate],
    sky_lookup: dict[tuple[str, date], float],
    current_datetime: datetime,
) -> Statuses:
    """Determine each (delegate, poll) participation status.

    Fetches every poll's votes from vote.sky.money (concurrently) and runs determine_vote_status against each delegate
    using their SKY balance from sky_lookup. Alignment timing overrides that rule: a poll that closed before a
    delegate's start date is "Not Started", and a delegate who exited before the poll closed is "Exited" unless they
    voted while still aligned, in which case the vote counts as normal.

    Raises:
        ValueError: if a poll has no end date, which the polls endpoint always supplies.
    """
    if not polls:
        return {}

    with ThreadPoolExecutor(max_workers=_POLL_VOTER_FETCH_CONCURRENCY) as executor:
        votes_by_poll = dict(executor.map(_fetch_poll_votes, polls))
    logger.info("Fetched votes for %d polls", len(votes_by_poll))

    statuses: Statuses = {}
    for poll in polls:
        if poll.end is None:
            msg = f"Poll {poll.id} has no end date"
            raise ValueError(msg)
        votes = votes_by_poll[poll.id]
        window = pd.date_range(poll.start, poll.end, freq="D").date

        for delegate in delegates:
            contract = delegate.vote_delegate_address
            voted = voted_while_aligned(votes.get(contract), delegate.end_date)
            if delegate.start_date > poll.end:
                statuses[contract, poll.id] = NOT_STARTED
            elif exited_before(delegate.end_date, poll.end) and not voted:
                statuses[contract, poll.id] = EXITED
            else:
                sky_by_date = {d: sky_lookup.get((contract, d), 0.0) for d in window}
                statuses[contract, poll.id] = determine_vote_status(
                    sky_by_date, poll.end, delegate_voted=voted, current_datetime=current_datetime
                )

    return statuses
