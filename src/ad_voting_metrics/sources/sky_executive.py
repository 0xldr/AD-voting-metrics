"""vote.sky.money executive endpoint: spell listing for a period, plus the balance-derived seed statuses."""

import logging
from datetime import date, datetime

import pandas as pd

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.vote_status import NO_DELEGATED_SKY, NOT_STARTED, PENDING_VERIFICATION

from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_EXECUTIVE_URL = "https://vote.sky.money/api/executive"

# The API's default page size for the executive listing.
SKY_EXECUTIVES_PAGE_SIZE = 100

# Safety cap on the executives loop - the endpoint paginates by absolute
# `start` index, so a bug returning non-empty pages forever would otherwise
# spin without bound. Real runs exit far earlier.
SKY_EXECUTIVES_PAGINATION_HARD_CAP = 10_000_000


def fetch_spells_for_period(period: MonthPeriod) -> list[dict]:
    """Fetch executive spells from vote.sky.money that went live within the period.

    The listing is newest-first, so paging stops at the first spell dated before the period.

    Returns:
        List of dicts with address, startDate (as `date`), and title.
    """
    spell_info: list[dict] = []
    for start in range(0, SKY_EXECUTIVES_PAGINATION_HARD_CAP, SKY_EXECUTIVES_PAGE_SIZE):
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
            if date_execute < period.start:
                return spell_info
            if date_execute <= period.end:
                spell_info.append(
                    {
                        "address": execute["address"].lower(),
                        "startDate": date_execute,
                        "title": execute["title"],
                    }
                )

    return spell_info


def add_spell_vote_statuses(
    spell_info: list[dict],
    df: pd.DataFrame,
    sky_lookup: dict[tuple[str, date], float],
) -> pd.DataFrame:
    """Add one column per spell to df, seeding each delegate's vote status.

    Only the statuses that follow from SKY balance and alignment dates are decided here:

      - No SKY delegated on the spell's start day -> "No Delegated SKY"
      - Aligned after the spell went live         -> "Not Started"
      - Otherwise                                 -> "Pending verification"

    Whether a delegate actually voted, and whether they did so inside the 3-business-day deadline, is settled by
    `sky_executive_onchain` against chief Vote events. The public supporters endpoint reports only who currently
    supports a spell, with no timestamp, so it cannot answer the deadline question and is not consulted.

    Returns:
        The same df, mutated in place with one new column per spell. Returns df unchanged when spell_info is empty.
    """
    for spell in spell_info:
        vote_statuses = []
        spell_address = spell["address"]
        start_date = spell["startDate"]

        for _, row in df.iterrows():
            address = row["Delegate Contract"]
            first_delegate_date = row["Start Date"]

            sky_on_start = sky_lookup.get((address, start_date), 0.0)

            voted = PENDING_VERIFICATION if sky_on_start != 0 else NO_DELEGATED_SKY

            if first_delegate_date > start_date:
                voted = NOT_STARTED

            vote_statuses.append(voted)

        df[str(spell_address)] = vote_statuses

    return df
