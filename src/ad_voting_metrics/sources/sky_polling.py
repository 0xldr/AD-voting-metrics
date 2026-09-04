"""vote.sky.money polling endpoints: poll listing + per-poll voter tallies."""

import itertools
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.vote_status import NOT_STARTED, Statuses, determine_vote_status

from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_ALL_POLLS_URL = "https://vote.sky.money/api/polling/all-polls"
SKY_POLL_ID_URL = "https://vote.sky.money/api/polling/tally"

# The API's default page size for the all-polls endpoint.
SKY_POLL_PAGE_SIZE = 30

# Max concurrent voter-list fetches in poll_statuses. vote.sky.money
# tolerates this comfortably; raising it gives diminishing returns.
_POLL_VOTER_FETCH_CONCURRENCY = 8


def fetch_polls_for_period(period: MonthPeriod) -> list[Ballot]:
    """Fetch polls from vote.sky.money that started within the period.

    The request's startDate parameter sets the lower bound and the listing comes back oldest-first, so paging stops at
    the first poll that starts after the period.

    Returns:
        Polls starting within the period, as Ballots.
    """
    polls: list[Ballot] = []
    for page in itertools.count(1):
        params: dict[str, str | int] = {
            "network": "mainnet",
            "pageSize": SKY_POLL_PAGE_SIZE,
            "page": page,
            "orderBy": "FURTHEST_START",
            "startDate": period.start.isoformat(),
        }
        response = get_session().get(
            SKY_ALL_POLLS_URL,
            params=params,
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        page_polls = data.get("polls", [])
        pagination = data.get("paginationInfo") or {}
        if not page_polls or not pagination:
            break

        for poll in page_polls:
            start = datetime.fromisoformat(poll["startDate"]).date()
            if start > period.end:
                return polls
            if start >= period.start:
                polls.append(
                    Ballot(
                        id=str(poll["pollId"]),
                        kind="poll",
                        start=start,
                        end=datetime.fromisoformat(poll["endDate"]).date(),
                        title=poll["title"],
                    )
                )

        if page >= pagination.get("numPages", page):
            break

    return polls


def _fetch_poll_voters(poll: Ballot) -> tuple[str, set[str]]:
    """Fetch the voter address set for one poll.

    Returns a tuple of (poll id, lowercased voter addresses).
    """
    response = get_session().get(
        f"{SKY_POLL_ID_URL}/{poll.id}",
        params={"network": "mainnet"},
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return poll.id, {vote["voter"].lower() for vote in data.get("votesByAddress", [])}


def poll_statuses(
    polls: list[Ballot],
    delegates: list[Delegate],
    sky_lookup: dict[tuple[str, date], float],
    current_datetime: datetime,
) -> Statuses:
    """Determine each (delegate, poll) participation status.

    Fetches every poll's voter list from vote.sky.money (concurrently) and runs determine_vote_status against each
    delegate using their SKY balance from sky_lookup. A poll that closed before a delegate's alignment start date is
    "Not Started".

    Returns:
        Mapping of (delegate contract, poll id) to status; empty when there are no polls.

    Raises:
        ValueError: if a poll has no end date, which the polls endpoint always supplies.
    """
    if not polls:
        return {}

    with ThreadPoolExecutor(max_workers=_POLL_VOTER_FETCH_CONCURRENCY) as executor:
        voters_by_poll = dict(executor.map(_fetch_poll_voters, polls))

    statuses: Statuses = {}
    for poll in polls:
        if poll.end is None:
            msg = f"Poll {poll.id} has no end date"
            raise ValueError(msg)
        voters = voters_by_poll[poll.id]
        window = pd.date_range(poll.start, poll.end, freq="D").date

        for delegate in delegates:
            contract = delegate.vote_delegate_address
            sky_by_date = {d: sky_lookup[contract, d] for d in window if (contract, d) in sky_lookup}
            status = determine_vote_status(
                sky_by_date, poll.end, delegate_voted=contract in voters, current_datetime=current_datetime
            )
            if delegate.start_date > poll.end:
                status = NOT_STARTED
            statuses[contract, poll.id] = status

    return statuses
