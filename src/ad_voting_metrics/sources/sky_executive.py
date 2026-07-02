"""vote.sky.money executive endpoints: spell listing + per-spell supporter lookups."""

import itertools
import logging
from datetime import date, datetime

import pandas as pd

from ad_voting_metrics.metrics import PENDING_VERIFICATION
from ad_voting_metrics.period import MonthPeriod

from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_EXECUTIVE_URL = "https://vote.sky.money/api/executive"
SKY_EXECUTIVE_SUPPORTERS_URL = "https://vote.sky.money/api/executive/supporters"

# The API's default page size for the executive listing.
SKY_EXECUTIVES_PAGE_SIZE = 100

# Safety cap on the executives loop - the endpoint paginates by absolute
# `start` index, so a bug returning non-empty pages forever would otherwise
# spin without bound. Real runs exit far earlier on empty data.
SKY_EXECUTIVES_PAGINATION_HARD_CAP = 10_000_000


def fetch_spells_for_period(period: MonthPeriod) -> list[dict]:
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


def add_spell_vote_statuses(
    spell_info: list[dict],
    df: pd.DataFrame,
    sky_lookup: dict[tuple[str, date], float],
) -> pd.DataFrame:
    """Add one column per spell to df, populated with each delegate's vote status.

    Returns:
        The same df, mutated in place with one new column per spell. Returns df unchanged (and skips the supporters HTTP
        call) when spell_info is empty.
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
                voted = "Yes" if address in supporter_set else PENDING_VERIFICATION
            else:
                voted = "No Delegated SKY"

            if first_delegate_date > start_date:
                voted = "Not Started"

            vote_statuses.append(voted)

        df[str(spell_address)] = vote_statuses

    return df
