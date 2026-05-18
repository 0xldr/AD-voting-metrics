"""Data access: Dune for daily SKY balances, vote.sky.money for polls and spells.

Owns the close-day vote-status rule (see `determine_vote_status`).
"""

import logging
import os
from datetime import UTC, date, datetime

import pandas as pd
from dateutil import parser
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


def get_all_sky_delegated(cache_max_age_hours: int | None = None) -> pd.DataFrame:
    """Fetch daily SKY delegations from Dune, indexed on (contract, date).

    With cache_max_age_hours set, uses Dune's get_latest_result and reuses
    a cached execution if it's within the threshold; otherwise executes
    fresh. Useful during developmentto avoid burning Dune credits.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with one
        running_total_balance column. delegation_contract is lowercased;
        dt is a YYYY-MM-DD string.

    Raises:
        RuntimeError: if DUNE_API_KEY is unset.
    """
    api_key = os.environ.get("DUNE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DUNE_API_KEY environment variable is not set. "
            "Add it to your .env file (see .env.example)."
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
    df["dt"] = df["dt"].astype(str)
    return df.set_index(["delegation_contract", "dt"])


def get_sky_delegated(data: pd.DataFrame, contract_address: str, dt: date) -> float:
    """Return the running total SKY balance for a (delegate, date) pair, or 0 if absent."""
    key = (contract_address.strip().lower(), dt.strftime("%Y-%m-%d"))
    try:
        value = data.loc[key, "running_total_balance"]
    except KeyError:
        return 0

    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning(
            "Non-numeric running_total_balance for %s on %s: %r",
            contract_address,
            dt,
            value,
        )
        return 0


def get_delegate_list_sky(df, period: MonthPeriod, cache_max_age_hours: int | None = None):
    """Build per-day SKY-delegation rows for each delegate across the period.

    Missing daily rows from Dune are filled with zero.

    Returns:
        A (delegate_list_sky, delegate_list_rank) tuple where:
          - delegate_list_sky is keyed by (contract, date) with daily SKY balance
          - delegate_list_rank is keyed by (name, date) with totals for ranking
    """
    all_sky_delegated = get_all_sky_delegated(cache_max_age_hours=cache_max_age_hours)

    days = list(pd.date_range(period.start, period.end, freq="D").date)

    delegate_list_sky = []
    delegate_list_rank = []
    for _index, row in df.iterrows():
        delegate_name = row["Delegate Name"].strip().lower()
        delegate_contract = row["Delegate Contract"].lower()
        for current_date in days:
            sky_delegated = get_sky_delegated(all_sky_delegated, delegate_contract, current_date)
            delegate_list_sky.append(
                {"contract": delegate_contract, "sky": sky_delegated, "date": current_date},
            )
            delegate_list_rank.append(
                {
                    "Delegate": delegate_name,
                    "Total Delegation": round(sky_delegated, 2),
                    "Rank": 1,
                    "Date": current_date,
                },
            )
    return delegate_list_sky, delegate_list_rank


def get_poll_ids(period: MonthPeriod):
    """Fetch polls from vote.sky.money that started within the period.

    Each poll has startDate and endDate normalized to `date` objects.

    Returns:
        List of poll dicts, filtered to those starting within the period.
    """
    poll_info = []
    page = 0
    all_found = False
    while all_found is False:
        page = page + 1
        base_url = f"{SKY_ALL_POLLS_URL}?network=mainnet&pageSize={SKY_POLL_PAGE_SIZE}&page={page}&orderBy=FURTHEST_START&startDate={period.start.isoformat()}"
        response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)

        response.raise_for_status()
        data = response.json()
        pagination_info = data.get("paginationInfo", [])
        polls = data.get("polls", [])

        if not pagination_info:
            break
        if not polls:
            break

        for poll in polls:
            start_date_poll = parser.parse(poll["startDate"]).date()

            if period.start <= start_date_poll <= period.end:
                poll["startDate"] = start_date_poll
                poll["endDate"] = parser.parse(poll["endDate"]).date()
                poll_info.append(poll)

        if pagination_info["numPages"] == 1:
            all_found = True

        if pagination_info["numPages"] == page:
            all_found = True

    return poll_info


def determine_vote_status(
    sky_by_date: dict[date, float],
    poll_end_date: date,
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
    poll_close_at = datetime(
        poll_end_date.year,
        poll_end_date.month,
        poll_end_date.day,
        16,
        0,
        0,
        tzinfo=UTC,
    )

    if current_datetime < poll_close_at:
        return "Yes" if delegate_voted else "Voting Open"

    if delegate_voted:
        return "Yes"

    close_day_sky = sky_by_date.get(poll_end_date, 0.0)
    had_sky_before_close = any(sky != 0 for d, sky in sky_by_date.items() if d < poll_end_date)

    if close_day_sky == 0 or not had_sky_before_close:
        return "No Delegated SKY"

    return "No"


def get_vote_poll_ids(poll_info, df, df_sky, current_datetime: datetime):
    """Add one column per poll to df, populated with each delegate's vote status.

    For every poll in poll_info, queries vote.sky.money for the voter
    list and runs determine_vote_status against each delegate using
    their SKY balance in df_sky. Polls that started before a delegate's
    alignment date are marked "Not Started".

    Returns:
        The same df, mutated in place with one new column per poll.
    """
    for poll in poll_info:
        vote_statuses = []
        base_url = f"{SKY_POLL_ID_URL}/{poll['pollId']}?network=mainnet"
        response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)

        response.raise_for_status()
        data = response.json()
        for _index, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = datetime.strptime(row["Start Date"], "%Y-%m-%d").date()
            delegate_voted = any(
                voter["voter"].lower() == address.lower()
                for voter in data.get("votesByAddress", [])
            )

            start_date = poll["startDate"]
            end_date = poll["endDate"]

            delegates_sky_available = df_sky[
                (df_sky["contract"].str.lower() == address.lower())
                & (df_sky["date"] >= start_date)
                & (df_sky["date"] <= end_date)
            ]

            sky_by_date: dict[date, float] = {
                row_sky["date"]: float(row_sky["sky"])
                for _, row_sky in delegates_sky_available.iterrows()
            }
            status = determine_vote_status(sky_by_date, end_date, delegate_voted, current_datetime)

            if first_delegate_date > end_date:
                status = "Not Started"

            vote_statuses.append(status)

        df[str(poll["pollId"])] = vote_statuses

    return df


def get_executive_ids(period: MonthPeriod):
    """Fetch executive spells from vote.sky.money that occurred within the period.

    Returns:
        List of dicts with address, startDate (as `date`), and title.
    """
    spell_info = []
    start = 0
    limit = SKY_EXECUTIVES_PAGE_SIZE
    while start < SKY_EXECUTIVES_PAGINATION_HARD_CAP:
        base_url = f"{SKY_EXECUTIVE_URL}?start={start}&limit={limit}"
        response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)

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

        start = start + limit

    return spell_info


def get_vote_executive_ids(spell_info, df, df_sky):
    """Add one column per spell to df, populated with each delegate's vote status.

    Returns:
        The same df, mutated in place with one new column per spell.
    """
    base_url = f"{SKY_EXECUTIVE_SUPPORTERS_URL}?network=mainnet"
    response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    for spell in spell_info:
        vote_statuses = []
        spell_address = spell["address"]
        start_date = spell["startDate"]

        for _index, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = datetime.strptime(row["Start Date"], "%Y-%m-%d").date()

            # `voted` is a bool from the supporters check, then gets
            # rewritten to a status string in the SKY-availability loop.
            voted: bool | str
            if spell_address in data:
                voted = any(
                    supporters["address"] == address.lower() for supporters in data[spell_address]
                )
            else:
                voted = False

            delegates_sky_available = df_sky[df_sky["contract"].str.lower() == address.lower()]

            for _index, delegate_sky_available in delegates_sky_available.iterrows():
                if delegate_sky_available["date"] != start_date:
                    continue

                if delegate_sky_available["sky"] != 0:
                    voted = "Yes" if voted else "Pending verification"
                    break
                voted = "No Delegated SKY"

            if first_delegate_date > start_date:
                voted = "Not Started"

            vote_statuses.append(voted)

        df[str(spell_address)] = vote_statuses

    return df


def custom_sort(df, hardcoded_order, poll_info, spell_info):
    """Reshape and reorder the per-poll/spell df for participation output.

    Drops working columns, adds blank rows for delegates in
    hardcoded_order that don't appear in df, sorts rows by hardcoded_order
    position (unknowns at the end), and transposes so each row is a
    poll or a spell.

    Returns:
        DataFrame whose first column is "Poll Id"and whose remaining
        columns are delegate-keyed status values.
    """
    df = df.drop(["Start Date"], axis=1)

    df.insert(df.columns.get_loc("Delegate Contract") + 1, "Delegate", df["Delegate Name"])
    df = df.drop(["Delegate Name"], axis=1)

    column_names = df.columns

    poll_by_id = {str(p["pollId"]): p for p in poll_info}
    spell_by_addr = {str(s["address"]): s for s in spell_info}

    title_list = []
    start_date_list = []
    end_date_list = []

    for column_name in column_names:
        key = str(column_name)
        object_found = poll_by_id.get(key) or spell_by_addr.get(key)

        if object_found:
            title_list.append(object_found["title"])

            start_date_list.append(object_found["startDate"])
            end_date_list.append(object_found.get("endDate", "N/A"))

        else:
            title_list.append("Title")
            start_date_list.append("Start Date")
            # Spells have no endDate; use "N/A" to keep column lengths aligned.
            end_date_list.append("End Date")

    missing_rows = [row for row in hardcoded_order if row.lower() not in df["Delegate Contract"].str.lower().tolist()]

    num_columns = len(df.columns)
    for row in missing_rows:
        blank_row = [None] * num_columns
        blank_row[0] = row
        df.loc[len(df)] = blank_row

    sort_keys = {addr.lower(): i for i, addr in enumerate(hardcoded_order)}
    df["SortKey"] = df["Delegate Contract"].str.lower().map(sort_keys).fillna(len(hardcoded_order)).astype(int)

    sorted_df = df.sort_values(by="SortKey")
    sorted_df = sorted_df.drop(columns=["SortKey"])
    sorted_df = sorted_df.rename(columns={"Delegate Contract": "", "Delegate": "Poll Id"})

    transposed_df = sorted_df.transpose()
    transposed_df.insert(0, "Start Date", start_date_list)
    transposed_df.insert(1, "End Date", end_date_list)
    transposed_df.insert(2, "Title", title_list)

    return transposed_df
