import logging
import os
from datetime import datetime, timedelta
from typing import Any

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


# Define a function to retrieve the SKY for each delegate by date.
def get_all_sky_delegated():
    """Fetch SKY delegations per (delegate, date) from Dune.

    Uses dune-client to execute query DUNE_SKY_QUERY_ID fresh on every call.
    Fresh execution costs Dune credits but ensures the script's monthly
    output reflects the latest on-chain delegation state.

    Reads DUNE_API_KEY from the environment. Raises RuntimeError with a
    clear message if unset, rather than letting dune-client fail with an
    opaque auth error from inside the SDK.

    Returns: list of dicts with columns delegation_contract, dt, and runnint_total_balance.
    """
    api_key = os.environ.get("DUNE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DUNE_API_KEY environment variable is not set. "
            "Add it to your .env file (see .env.example)."
        )

    dune = DuneClient(api_key=api_key)
    query = QueryBase(query_id=DUNE_SKY_QUERY_ID)
    logger.info("Executing Dune query %d (fresh)...", DUNE_SKY_QUERY_ID)
    results = dune.run_query(query=query)
    rows = results.get_rows()
    logger.info("Dune query returned %d rows", len(rows))
    return rows


def get_sky_delegated(data, contract_address, date):
    # Loop through each item in the list
    for item in data:
        # Check if both the contract and date match

        if (
            item.get("delegation_contract").strip().lower() == contract_address.strip().lower()
            and datetime.strptime(item.get("dt"), "%Y-%m-%d").date() == date
        ):
            # Return the running total balance for that match
            return item.get("running_total_balance")

    # Return 0 if no match is found
    return 0


# Define a function to retrieve the total SKY held by each delegate by date.
def get_delegate_list_sky(df, period: MonthPeriod):

    all_sky_delegated = get_all_sky_delegated()

    delegate_data_sky: dict[str, dict[str, dict[Any, Any]]] = {"contract": {}, "name": {}}
    for _index, row in df.iterrows():
        delegate_name = row["Delegate Name"].strip().lower()
        delegate_contract = row["Delegate Contract"]
        if delegate_name not in delegate_data_sky["name"]:
            delegate_data_sky["name"][delegate_name] = {}

        if delegate_contract not in delegate_data_sky["contract"]:
            delegate_data_sky["contract"][delegate_contract] = {}

        current_date = period.start
        while current_date <= period.end:
            if current_date.strftime("%Y-%m-%d") not in delegate_data_sky["name"][delegate_name]:
                delegate_data_sky["name"][delegate_name][current_date.strftime("%Y-%m-%d")] = {
                    "sky": 0
                }
            if (
                current_date.strftime("%Y-%m-%d")
                not in delegate_data_sky["contract"][delegate_contract]
            ):
                delegate_data_sky["contract"][delegate_contract][current_date] = {"sky": 0}

            sky_delegated = get_sky_delegated(all_sky_delegated, delegate_contract, current_date)

            delegate_data_sky["name"][delegate_name][current_date.strftime("%Y-%m-%d")]["sky"] += (
                sky_delegated
            )
            delegate_data_sky["contract"][delegate_contract][current_date]["sky"] = sky_delegated

            current_date += timedelta(days=1)

    delegate_list_rank = []
    for delegate_name, data in delegate_data_sky["name"].items():
        for date, data_sky in data.items():
            delegate_list_rank.append(
                {
                    "Delegate": delegate_name,
                    "Total Delegation": round(data_sky["sky"], 2),
                    "Rank": 1,
                    "Date": date,
                }
            )
    delegate_list_sky = []
    for delegate_contract, data in delegate_data_sky["contract"].items():
        for date, data_sky in data.items():
            delegate_list_sky.append(
                {"contract": delegate_contract.lower(), "sky": data_sky["sky"], "date": date}
            )

    return delegate_list_sky, delegate_list_rank


# define a function to get the polls IDs for Data.
def get_poll_ids(period: MonthPeriod):
    poll_info = []
    page = 0
    all_found = False
    while all_found is False:
        page = page + 1
        base_url = f"{SKY_ALL_POLLS_URL}?network=mainnet&pageSize=30&page={page}&orderBy=FURTHEST_START&startDate={period.start.isoformat()}"
        response = get_session().get(base_url, headers=HEADERS, timeout=HTTP_TIMEOUT)

        response.raise_for_status()
        data = response.json()
        # Make the API request
        paginationInfo = data.get("paginationInfo", [])
        polls = data.get("polls", [])

        if not paginationInfo:
            break
        if not polls:
            break

        for poll in polls:
            start_date_poll = parser.parse(poll["startDate"]).date()

            if period.start <= start_date_poll <= period.end:
                poll_info.append(poll)

        if paginationInfo["numPages"] == 1:
            all_found = True

        if paginationInfo["numPages"] == page:
            all_found = True

    return poll_info


# Define a function to confirm the voting of each delegate in the conducted polls.
def get_vote_poll_ids(poll_info, df, df_sky):
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
            voted = any(
                voter["voter"].lower() == address.lower()
                for voter in data.get("votesByAddress", [])
            )

            start_date = parser.parse(poll["startDate"]).date()
            end_date = parser.parse(poll["endDate"]).date()

            delegates_sky_available = df_sky[
                (df_sky["contract"].str.lower() == address.lower())
                & (df_sky["date"] >= start_date)
                & (df_sky["date"] <= end_date)
            ]

            for _index, delegate_sky_available in delegates_sky_available.iterrows():
                if delegate_sky_available["sky"] != 0:
                    voted = "Yes" if voted else "No"
                    break
                voted = "No Delegated SKY"

            if first_delegate_date > end_date:
                voted = "Not Started"

            vote_statuses.append(voted)

        # Add a new column to the DataFrame with the poll id as the header
        df[str(poll["pollId"])] = vote_statuses

    return df


# define a function to get the executes IDs for Data.
def get_execute_ids(period: MonthPeriod):
    spell_info = []
    start = 0
    limit = 100
    while start < 10000000:
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
                spell_info.append(
                    {
                        "address": execute["address"].lower(),
                        "startDate": date_execute,
                        "title": execute["title"],
                    }
                )

        start = start + limit

    return spell_info


# Define a function to confirm the voting of each delegate in the spells.
def get_vote_execute_ids(spell_info, df, df_sky):
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
    # Define your hardcoded order array
    # Drop the columns we received but don't need in the per-poll/spell view.
    df = df.drop(["Start Date"], axis=1)

    # Build the human-readable Delegate column
    df.insert(df.columns.get_loc("Delegate Contract") + 1, "Delegate", df["Delegate Name"])
    df.drop(["Delegate Name"], axis=1, inplace=True)

    # Get the names of all columns in the DataFrame
    column_names = df.columns

    # Lists to store the values
    title_list = []
    startDate_list = []
    endDate_list = []

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

            if isinstance(object_found["startDate"], str):
                startDate_list.append(parser.parse(object_found["startDate"]).date())
            else:
                startDate_list.append(object_found["startDate"])

            try:
                if isinstance(object_found["endDate"], str):
                    endDate_list.append(parser.parse(object_found["endDate"]).date())
                else:
                    endDate_list.append(object_found["endDate"])
            except Exception:
                endDate_list.append("N/A")

        else:
            title_list.append("Title")
            startDate_list.append("Start Date")
            endDate_list.append("End Date")

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
    sorted_df.drop(columns=["SortKey"], inplace=True)

    sorted_df.rename(columns={"Delegate Contract": "", "Delegate": "Poll Id"}, inplace=True)

    transposed_df = sorted_df.transpose()

    transposed_df.insert(0, "Start Date", startDate_list)
    transposed_df.insert(1, "End Date", endDate_list)
    transposed_df.insert(2, "Title", title_list)

    return transposed_df
