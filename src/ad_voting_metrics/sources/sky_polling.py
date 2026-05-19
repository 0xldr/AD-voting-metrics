"""vote.sky.money polling endpoints: poll listing + per-poll voter tallies."""

import itertools
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.vote_status import determine_vote_status

from .dune import build_sky_lookup
from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_ALL_POLLS_URL = "https://vote.sky.money/api/polling/all-polls"
SKY_POLL_ID_URL = "https://vote.sky.money/api/polling/tally"

# The API's default page size for the all-polls endpoint.
SKY_POLL_PAGE_SIZE = 30

# Max concurrent voter-list fetches in get_vote_poll_ids. vote.sky.money
# tolerates this comfortably; raising it gives diminishing returns.
_POLL_VOTER_FETCH_CONCURRENCY = 8


def get_poll_ids(period: MonthPeriod) -> list[dict]:
    """Fetch polls from vote.sky.money that started within the period.

    Each poll has startDate and endDate normalized to `date` objects.

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
            if period.start <= start_date_poll <= period.end:
                poll["startDate"] = start_date_poll
                poll["endDate"] = datetime.fromisoformat(poll["endDate"]).date()
                poll_info.append(poll)

        if page >= pagination.get("numPages", page):
            break

    return poll_info


def _fetch_poll_voters(poll: dict) -> tuple[int, set[str]]:
    """Fetch the voter address set for one poll.

    Returns:
        Tuple of (pollId, lowercased voter addresses).
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


def get_vote_poll_ids(
    poll_info: list[dict], df: pd.DataFrame, df_sky: pd.DataFrame, current_datetime: datetime
) -> pd.DataFrame:
    """Add one column per poll to df, populated with each delegate's vote status.

    For every poll in poll_info, queries vote.sky.money for the voter
    list and runs determine_vote_status against each delegate using
    their SKY balance in df_sky. Polls that started before a delegate's
    alignment date are marked "Not Started".

    Returns:
        The same df, mutated in place with one new column per poll.
    """
    if not poll_info:
        return df

    sky_lookup = build_sky_lookup(df_sky)
    first_dates_by_contract = dict(
        zip(df["Delegate Contract"], df["Start Date"].map(date.fromisoformat), strict=True),
    )

    with ThreadPoolExecutor(max_workers=_POLL_VOTER_FETCH_CONCURRENCY) as executor:
        voter_sets = dict(executor.map(_fetch_poll_voters, poll_info))

    for poll in poll_info:
        voter_set = voter_sets[poll["pollId"]]
        start_date = poll["startDate"]
        end_date = poll["endDate"]
        poll_window_days = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

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
                status = "Not Started"

            vote_statuses.append(status)

        df[str(poll["pollId"])] = vote_statuses

    return df
