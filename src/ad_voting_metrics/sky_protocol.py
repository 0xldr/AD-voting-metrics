"""Data access: Dune for daily SKY balances, vote.sky.money for polls and spells.

Owns the close-day vote-status rule (see `determine_vote_status`).
"""

import itertools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
from dune_client.client import DuneClient
from dune_client.query import QueryBase

from .period import MonthPeriod
from .sources.http import HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

DUNE_SKY_QUERY_ID = 6604139
SKY_ALL_POLLS_URL = "https://vote.sky.money/api/polling/all-polls"
SKY_EXECUTIVE_SUPPORTERS_URL = "https://vote.sky.money/api/executive/supporters"
SKY_POLL_ID_URL = "https://vote.sky.money/api/polling/tally"
SKY_EXECUTIVE_URL = "https://vote.sky.money/api/executive"

# Page sizes for sky.money's paginated endpoints. The API's defaults.
SKY_POLL_PAGE_SIZE = 30
SKY_EXECUTIVES_PAGE_SIZE = 100

# Safety cap on the executives loop - the endpoint paginates by absolute
# `start` index, so a bug returning non-empty pages forever would otherwise
# spin without bound. Real runs exit far earlier on empty data.
SKY_EXECUTIVES_PAGINATION_HARD_CAP = 10_000_000

# Max concurrent voter-list fetches in get_vote_poll_ids. vote.sky.money
# tolerates this comfortably; raising it gives diminishing returns.
_POLL_VOTER_FETCH_CONCURRENCY = 8


def get_all_sky_delegated(cache_max_age_hours: int | None = None) -> pd.DataFrame:
    """Fetch daily SKY delegations from Dune, indexed on (contract, date).

    With cache_max_age_hours set, uses Dune's get_latest_result and reuses
    a cached execution if it's within the threshold; otherwise executes
    fresh. Useful during development to avoid burning Dune credits.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with one
        running_total_balance column. delegation_contract is lowercased;
        dt is a datetime.date.

    Raises:
        RuntimeError: if DUNE_API_KEY is unset.
    """
    api_key = os.environ.get("DUNE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DUNE_API_KEY environment variable is not set. Add it to your .env file (see .env.example).",
        )

    dune = DuneClient(api_key=api_key)
    query = QueryBase(query_id=DUNE_SKY_QUERY_ID)

    df: pd.DataFrame
    if cache_max_age_hours is None:
        logger.info("Executing Dune query %d (fresh)...", DUNE_SKY_QUERY_ID)
        df = dune.run_query_dataframe(query=query)
    else:
        logger.info(
            "Fetching Dune query %d (cached up to %dh; will execute fresh if older)...",
            DUNE_SKY_QUERY_ID,
            cache_max_age_hours,
        )
        results = dune.get_latest_result(query=query, max_age_hours=cache_max_age_hours)
        df = pd.DataFrame(results.get_rows())
    logger.info("Dune query returns %d rows", len(df))

    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    df["running_total_balance"] = pd.to_numeric(df["running_total_balance"], errors="coerce").fillna(0.0)
    return df.set_index(["delegation_contract", "dt"])


def get_delegate_list_sky(
    df: pd.DataFrame, period: MonthPeriod, cache_max_age_hours: int | None = None
) -> pd.DataFrame:
    """Build one row per (delegate, day) with SKY balance for the period.

    Missing daily rows from Dune are filled with zero.

    Returns:
        DataFrame with columns: contract, name, date, sky. One row per
        (delegate, day) covering every day in the period.
    """
    all_sky_delegated = get_all_sky_delegated(cache_max_age_hours=cache_max_age_hours)

    days = list(pd.date_range(period.start, period.end, freq="D").date)
    contracts = df["Delegate Contract"].tolist()
    names_by_contract = {
        contract: name.strip().lower()
        for contract, name in zip(df["Delegate Contract"], df["Delegate Name"], strict=True)
    }

    target = pd.MultiIndex.from_product([contracts, days], names=["delegation_contract", "dt"])
    filled = (
        all_sky_delegated["running_total_balance"]
        .reindex(target, fill_value=0.0)
        .astype(float)
        .reset_index()
    )

    return pd.DataFrame({
        "contract": filled["delegation_contract"],
        "name": filled["delegation_contract"].map(names_by_contract),
        "date": filled["dt"],
        "sky": filled["running_total_balance"],
    })


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


def determine_vote_status(
    sky_by_date: dict[date, float],
    poll_end_date: date,
    *,
    delegate_voted: bool,
    current_datetime: datetime,
) -> str:
    """Determine the participation status for one (delegate, poll) pair.

    sky_by_date is the delegate's SKY balance per day across the voting
    window. Missing dates are treated as zero. poll_end_date is the
    poll's close day; voting on SKY polls ends at 16:00 UTC.

    Rule:

    - If the poll is still open (current_datetime < 16:00 UTC on
      poll_end_date), the result is still in flux:
        - Voted -> "Yes"
        - Not voted -> "Voting Open" (DISCOUNTED, doesn't penalize)
      A non-voter might still vote before close, so marking them "No"
      now would be wrong; re-running after close resolves the status.

    - If the poll has closed, apply the close-day rule. A delegate is
      on the hook for "No" only if BOTH hold:

        1. Non-zero SKY on the close day, AND
        2. Non-zero SKY on at least one prior day in the window.

      Otherwise the status is "No Delegated SKY":

        - Zero throughout window           -> No Delegated SKY
        - Had SKY at some point AND voted  -> Yes
        - SKY before AND at close, no vote -> No

    Without stake at close, a delegate can't be held responsible for
    not voting.

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


def _build_sky_lookup(df_sky: pd.DataFrame) -> dict[tuple[str, date], float]:
    """Materialize df_sky into a (contract, date) -> sky-balance dict for O(1) lookup.

    Returns:
        Mapping of (contract, date) to that day's SKY balance.
    """
    return {(r["contract"], r["date"]): float(r["sky"]) for r in df_sky.to_dict(orient="records")}


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

    sky_lookup = _build_sky_lookup(df_sky)
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


def get_executive_ids(period: MonthPeriod) -> list[dict]:
    """Fetch executive spells from vote.sky.money that occurred within the period.

    Returns:
        List of dicts with address, startDate (as `date`), and title.
    """
    spell_info: list[dict] = []
    for start in itertools.count(0, SKY_EXECUTIVES_PAGE_SIZE):
        if start >= SKY_EXECUTIVES_PAGINATION_HARD_CAP:
            break
        response = get_session().get(
            SKY_EXECUTIVE_URL,
            params={"start": start, "limit": SKY_EXECUTIVES_PAGE_SIZE},
            headers=HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            break

        for execute in data:
            date_execute = datetime.fromisoformat(execute["date"]).date()
            if period.start <= date_execute <= period.end:
                spell_info.append({
                    "address": execute["address"].lower(),
                    "startDate": date_execute,
                    "title": execute["title"],
                })

    return spell_info


def get_vote_executive_ids(spell_info: list[dict], df: pd.DataFrame, df_sky: pd.DataFrame) -> pd.DataFrame:
    """Add one column per spell to df, populated with each delegate's vote status.

    Returns:
        The same df, mutated in place with one new column per spell. Returns
        df unchanged (and skips the supporters HTTP call) when spell_info is empty.
    """
    if not spell_info:
        return df

    response = get_session().get(
        SKY_EXECUTIVE_SUPPORTERS_URL,
        params={"network": "mainnet"},
        headers=HEADERS,
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    supporters_by_spell = response.json()

    sky_lookup = _build_sky_lookup(df_sky)

    for spell in spell_info:
        vote_statuses = []
        spell_address = spell["address"]
        start_date = spell["startDate"]
        supporter_set = {s["address"].lower() for s in supporters_by_spell.get(spell_address, [])}

        for _, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = date.fromisoformat(row["Start Date"])

            sky_on_start = sky_lookup.get((address, start_date), 0.0)

            voted: str
            if sky_on_start != 0:
                voted = "Yes" if address in supporter_set else "Pending verification"
            else:
                voted = "No Delegated SKY"

            if first_delegate_date > start_date:
                voted = "Not Started"

            vote_statuses.append(voted)

        df[str(spell_address)] = vote_statuses

    return df
