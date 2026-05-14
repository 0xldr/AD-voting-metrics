"""Data access layer: Dune for daily SKY balances, vote.sky.money for polls and spells.

Fetches the raw inputs that the metrics pipeline operates on. Owns the
close-day vote-status rule (see `determine_vote_status`) and the
chronological sort that groups polls and spells in the participation
output.
"""

import logging
import os
from datetime import UTC, date, datetime, timedelta

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

SKY_POLL_PAGE_SIZE = 30
SKY_EXECUTIVES_PAGE_SIZE = 100
SKY_EXECUTIVES_PAGINATION_HARD_CAP = 10_000_000


# Define a function to retrieve the SKY for each delegate by date.
def get_all_sky_delegated(cache_max_age_hours: int | None = None) -> pd.DataFrame:
    """Fetch SKY delegations per (delegate, date) from Dune as an indexed DataFrame.

    By default (cache_max_age_hours=None), executes Dune query
    DUNE_SKY_QUERY_ID fresh - the safe-default for production runs.

    When cache_age_max_hours is set to a non-negative integer, uses
    dune-client's get_latest_result with the supplied threshold: returns
    the most recent cached execution if it's within N hours, otherwise
    triggers a fresh execution. Useful for fast development iteration
    where re-execution every run is wasteful credits and time.

    Reads DUNE_API_KEY from the environment.

    Returns:
        A DataFrame indexed on (delegation_contract, dt) with a
        running_total_balance column. delegation_contract is lowercased;
        dt is a string in YYYY-MM-DD form.

    Raises:
        RuntimeError: if DUNE_API_KEY is unset (clearer than the
            dune-client SDK's opaque auth error).
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

    # Normalize for indexed lookup
    df["delegation_contract"] = df["delegation_contract"].str.lower()
    df["dt"] = df["dt"].astype(str)
    return df.set_index(["delegation_contract", "dt"])


def get_sky_delegated(data: pd.DataFrame, contract_address: str, dt: date) -> float:
    """Return the running total SKY balance for a (delegate, date) pair.

    Returns 0 if the (contract, date) combination is not in the dataset.
    """
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


# Define a function to retrieve the total SKY held by each delegate by date.
def get_delegate_list_sky(df, period: MonthPeriod, cache_max_age_hours: int | None = None):
    """Build per-day SKY-delegation rows for each delegate across the period.

    Missing daily rows from Dune are filled with zero.

    Returns:
        A (delegate_list_sky, delegate_list_rank) tuple where:
          - delegate_list_sky is keyed by (contract, date) with daily SKY balance
          - delegate_list_rank is keyed by (name, date) with totals for ranking
    """
    all_sky_delegated = get_all_sky_delegated(cache_max_age_hours=cache_max_age_hours)

    delegate_data_sky: dict[str, dict[str, dict[date, dict[str, float]]]] = {
        "contract": {},
        "name": {},
    }
    for _index, row in df.iterrows():
        delegate_name = row["Delegate Name"].strip().lower()
        delegate_contract = row["Delegate Contract"]
        if delegate_name not in delegate_data_sky["name"]:
            delegate_data_sky["name"][delegate_name] = {}

        if delegate_contract not in delegate_data_sky["contract"]:
            delegate_data_sky["contract"][delegate_contract] = {}

        current_date = period.start
        while current_date <= period.end:
            if current_date not in delegate_data_sky["name"][delegate_name]:
                delegate_data_sky["name"][delegate_name][current_date] = {"sky": 0}
            if current_date not in delegate_data_sky["contract"][delegate_contract]:
                delegate_data_sky["contract"][delegate_contract][current_date] = {"sky": 0}

            sky_delegated = get_sky_delegated(all_sky_delegated, delegate_contract, current_date)

            delegate_data_sky["name"][delegate_name][current_date]["sky"] += sky_delegated
            delegate_data_sky["contract"][delegate_contract][current_date]["sky"] = sky_delegated

            current_date += timedelta(days=1)

    delegate_list_rank = []
    for delegate_name, data in delegate_data_sky["name"].items():
        for entry_date, data_sky in data.items():
            delegate_list_rank.append({
                "Delegate": delegate_name,
                "Total Delegation": round(data_sky["sky"], 2),
                "Rank": 1,
                "Date": entry_date,
            })
    delegate_list_sky = []
    for delegate_contract, data in delegate_data_sky["contract"].items():
        for entry_date, data_sky in data.items():
            delegate_list_sky.append({
                "contract": delegate_contract.lower(),
                "sky": data_sky["sky"],
                "date": entry_date,
            })

    return delegate_list_sky, delegate_list_rank


# define a function to get the polls IDs for Data.
def get_poll_ids(period: MonthPeriod):
    """Fetch all polls from vote.sky.money that started within the period.

    Paginates through the polls endpoint. Each poll dict has its
    startDate and endDate fields normalized to `date` objects in place
    so downstream consumers don't need to hanlde the API's string form.

    Returns:
        A list of poll dicts as returned by the API, filtered to those
        starting within the period and with date fields normalized.
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
        # Make the API request
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

    sky_by_date is the delegate's SKY balance per day across the voting window
    (typically 4 calendar days for a 3 day poll: 16:00-24:00 on day 0, full
    days 1 and 2, 0:00-16:00 on day 3). Missing dates are treated as zero.

    poll_end_date is the poll's close day. Voting on SKY polls ends at 16:00
    UTC on that day; this function constructs the close datetime internally
    for comparison.
    delegate_voted is True if the delegate cast a vote on this poll as of
    the latest API check.
    current_datetime is the timezone-aware datetime metrics are being computed
    at - used to detect polls still in their voting window at the time of the
    run.

    If the poll is still open (current_datetime < 16:00 UTC on
    poll_end_date), the result is still in flux. We treat in-progress
    polls positively:

    A delegate is counted as a "No" vote if BOTH of these criteria are true:
      1. They had non-zero SKY delegated on the close day, AND
      2. They had non-zero SKY delegated on at least one prior day in the
      window.

    If either fails, status is "No Delegated SKY".

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


# Define a function to confirm the voting of each delegate in the conducted polls.
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
        # Initialize an empty list to store vote status (Yes, Pending verification,No Delegated SKY or Not Started)
        vote_statuses = []
        # Make the API request
        base_url = f"{SKY_POLL_ID_URL}/{poll['pollId']}?network=mainnet"
        response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)

        response.raise_for_status()
        data = response.json()
        for _index, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = datetime.strptime(row["Start Date"], "%Y-%m-%d").date()
            # Check if the address voted in this poll
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

        # Add a new column to the DataFrame with the poll id as the header
        df[str(poll["pollId"])] = vote_statuses

    return df


# define a function to get the executive IDs for Data.
def get_executive_ids(period: MonthPeriod):
    """Fetch all executive spells from vote.sky.money that occurred within the period.

    Paginates through the executives endpoint.

    Returns:
        A list of dicts with address, startDate (as `date`), and title.
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
            date_execute = parser.parse(
                execute["date"].replace("(Coordinated Universal Time)", "")
            ).date()

            if period.start <= date_execute <= period.end:
                spell_info.append({
                    "address": execute["address"].lower(),
                    "startDate": date_execute,
                    "title": execute["title"],
                })

        start = start + limit

    return spell_info


# Define a function to confirm the voting of each delegate in the spells.
def get_vote_executive_ids(spell_info, df, df_sky):
    """Add one column per spell to df, populated with each delegate's vote status.

    Fetches the supporters list from vote.sky.money once, then for each
    spell checks each delegate's address against the supporters and
    cross-references with df_sky to assign Yes / Pending verification /
    No Delegated SKY / Not Started.

    Returns:
        The same df, mutated in place with one new column per spell.
    """
    base_url = f"{SKY_EXECUTIVE_SUPPORTERS_URL}?network=mainnet"
    # Make the API request
    response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()

    for spell in spell_info:
        # Initialize an empty list to store vote status (Yes, Pending verification,No Delegated SKY or Not Started)
        vote_statuses = []
        spell_address = spell["address"]
        start_date = spell["startDate"]

        for _index, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = datetime.strptime(row["Start Date"], "%Y-%m-%d").date()

            voted: bool | str
            if spell_address in data:
                # Check if the address voted in this spell
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

        # Add a new column to the DataFrame with the poll id as the header
        df[str(spell_address)] = vote_statuses

    return df


# Define the custom sorting function
def custom_sort(df, hardcoded_order, poll_info, spell_info):
    """Reshape and reorder the per-poll/spell df for participation output.

    Drops working columns, adds blank rows for delegates in
    hardcoded_order that don't appear in df, transposes so each row is
    a poll or a spell, and chronologically sorts those rows.

    Returns:
        A DataFrame whose first column is "Poll Id"and whose remaining
    columns are delegate-keyed status values.
    """
    # Define your hardcoded order array
    # Drop the columns we received but don't need in the per-poll/spell view.
    df = df.drop(["Start Date"], axis=1)

    # Build the human-readable Delegate column
    df.insert(df.columns.get_loc("Delegate Contract") + 1, "Delegate", df["Delegate Name"])
    df = df.drop(["Delegate Name"], axis=1)

    # Get the names of all columns in the DataFrame
    column_names = df.columns

    # Lists to store the values
    title_list = []
    start_date_list = []
    end_date_list = []

    # Search for objects and store values in lists
    for column_name in column_names:
        object_found = next(
            (obj for obj in poll_info if str(obj["pollId"]) == str(column_name)), None
        )
        if not object_found:
            object_found = next(
                (obj for obj in spell_info if str(obj["address"]) == str(column_name)), None
            )

        if object_found:
            title_list.append(object_found["title"])

            start_date_list.append(object_found["startDate"])
            end_date_list.append(object_found.get("endDate", "N/A"))

        else:
            title_list.append("Title")
            start_date_list.append("Start Date")
            end_date_list.append("End Date")

    # Identify the missing rows in hardcoded_order.
    missing_rows = [
        row
        for row in hardcoded_order
        if row.lower() not in df["Delegate Contract"].str.lower().tolist()
    ]

    num_columns = len(df.columns)
    # Add blank rows at the beginning of the DataFrame for the missing elements.
    for row in missing_rows:
        blank_row = [None] * num_columns
        blank_row[0] = row
        df.loc[len(df)] = blank_row  # Add a row with the contract and a null value for Age.

    # Create a new column for sorting based on the custom_sort function
    df["SortKey"] = df["Delegate Contract"].apply(
        lambda x: (
            hardcoded_order.index(x.lower())
            if x.lower() in hardcoded_order
            else len(hardcoded_order)
        )
    )

    # Sort the DataFrame using the SortKey column
    sorted_df = df.sort_values(by="SortKey")

    # Remove the SortKey column if no longer needed
    sorted_df = sorted_df.drop(columns=["SortKey"])

    sorted_df = sorted_df.rename(columns={"Delegate Contract": "", "Delegate": "Poll Id"})

    transposed_df = sorted_df.transpose()

    transposed_df.insert(0, "Start Date", start_date_list)
    transposed_df.insert(1, "End Date", end_date_list)
    transposed_df.insert(2, "Title", title_list)

    return transposed_df
