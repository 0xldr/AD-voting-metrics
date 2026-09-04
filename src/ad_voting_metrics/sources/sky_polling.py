"""vote.sky.money polling endpoints: poll listing + per-poll voter tallies."""

import itertools
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pandas as pd

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.vote_status import NOT_STARTED, determine_vote_status

from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_ALL_POLLS_URL = "https://vote.sky.money/api/polling/all-polls"
SKY_POLL_ID_URL = "https://vote.sky.money/api/polling/tally"

# The API's default page size for the all-polls endpoint.
SKY_POLL_PAGE_SIZE = 30

# Max concurrent voter-list fetches in add_poll_vote_statuses. vote.sky.money
# tolerates this comfortably; raising it gives diminishing returns.
_POLL_VOTER_FETCH_CONCURRENCY = 8


def fetch_polls_for_period(period: MonthPeriod) -> list[dict]:
    """Fetch polls from vote.sky.money that started within the period.

    The request's startDate parameter sets the lower bound and the listing comes back oldest-first, so paging stops at
    the first poll that starts after the period. Each poll has startDate and endDate normalized to `date` objects.

    Returns:
        List of poll dicts, filtered to those starting within the period.
    """
    poll_info: list[dict] = []
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
        polls = data.get("polls", [])
        pagination = data.get("paginationInfo") or {}
        if not polls or not pagination:
            break

        for poll in polls:
            start_date_poll = datetime.fromisoformat(poll["startDate"]).date()
            if start_date_poll > period.end:
                return poll_info
            if start_date_poll >= period.start:
                poll["startDate"] = start_date_poll
                poll["endDate"] = datetime.fromisoformat(poll["endDate"]).date()
                poll_info.append(poll)

        if page >= pagination.get("numPages", page):
            break

    return poll_info


def _fetch_poll_voters(poll: dict) -> tuple[int, set[str]]:
    """Fetch the voter address set for one poll.

    Returns a tuple of (pollId, lowercased voter addresses).
    """
    response = get_session().get(
        f"{SKY_POLL_ID_URL}/{poll['pollId']}",
        params={"network": "mainnet"},
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    return poll["pollId"], {vote["voter"].lower() for vote in data.get("votesByAddress", [])}


def add_poll_vote_statuses(
    poll_info: list[dict],
    df: pd.DataFrame,
    sky_lookup: dict[tuple[str, date], float],
    current_datetime: datetime,
) -> pd.DataFrame:
    """Add one column per poll to df, populated with each delegate's vote status.

    For every poll in poll_info, queries vote.sky.money for the voter list and runs determine_vote_status against each
    delegate using their SKY balance from sky_lookup. Polls that started before a delegate's alignment date are marked
    "Not Started".

    Returns:
        The same df, mutated in place with one new column per poll.
    """
    if not poll_info:
        return df

    first_dates_by_contract = dict(zip(df["Delegate Contract"], df["Start Date"], strict=True))

    with ThreadPoolExecutor(max_workers=_POLL_VOTER_FETCH_CONCURRENCY) as executor:
        voter_sets = dict(executor.map(_fetch_poll_voters, poll_info))

    for poll in poll_info:
        voter_set = voter_sets[poll["pollId"]]
        start_date = poll["startDate"]
        end_date = poll["endDate"]
        poll_window_days = pd.date_range(start_date, end_date, freq="D").date

        vote_statuses = []
        for _, row in df.iterrows():
            address = row["Delegate Contract"]
            delegate_voted = address in voter_set

            sky_by_date: dict[date, float] = {
                d: sky_lookup[address, d] for d in poll_window_days if (address, d) in sky_lookup
            }
            status = determine_vote_status(
                sky_by_date, end_date, delegate_voted=delegate_voted, current_datetime=current_datetime
            )

            if first_dates_by_contract[address] > end_date:
                status = NOT_STARTED

            vote_statuses.append(status)

        df[str(poll["pollId"])] = vote_statuses

    return df
