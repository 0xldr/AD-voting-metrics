"""vote.sky.money executive endpoint: spell listing for a period, plus the balance-derived seed statuses."""

import logging
from datetime import date, datetime

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate
from ad_voting_metrics.vote_status import NO_DELEGATED_SKY, NOT_STARTED, PENDING_VERIFICATION, Statuses

from .http import HEADERS, HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

SKY_EXECUTIVE_URL = "https://vote.sky.money/api/executive"

# The API's default page size for the executive listing.
SKY_EXECUTIVES_PAGE_SIZE = 100

# Safety cap on the executives loop - the endpoint paginates by absolute
# `start` index, so a bug returning non-empty pages forever would otherwise
# spin without bound. Real runs exit far earlier.
SKY_EXECUTIVES_PAGINATION_HARD_CAP = 10_000_000


def fetch_spells_for_period(period: MonthPeriod) -> list[Ballot]:
    """Fetch executive spells from vote.sky.money that went live within the period.

    The listing is newest-first, so paging stops at the first spell dated before the period.

    Returns:
        Spells going live within the period, as Ballots with no end date.
    """
    spells: list[Ballot] = []
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
            live = datetime.fromisoformat(execute["date"]).date()
            if live < period.start:
                return spells
            if live <= period.end:
                spells.append(
                    Ballot(
                        id=execute["address"].lower(),
                        kind="spell",
                        start=live,
                        end=None,
                        title=execute["title"],
                    )
                )

    return spells


def spell_statuses(
    spells: list[Ballot],
    delegates: list[Delegate],
    sky_lookup: dict[tuple[str, date], float],
) -> Statuses:
    """Seed each (delegate, spell) status from SKY balance and alignment dates.

      - Aligned after the spell went live         -> "Not Started"
      - No SKY delegated on the spell's start day -> "No Delegated SKY"
      - Otherwise                                 -> "Pending verification"

    Whether a delegate actually voted, and whether they did so inside the 3-business-day deadline, is settled by
    `sky_executive_onchain` against chief Vote events. The public supporters endpoint reports only who currently
    supports a spell, with no timestamp, so it cannot answer the deadline question and is not consulted.

    Returns:
        Mapping of (delegate contract, spell address) to status; empty when there are no spells.
    """
    statuses: Statuses = {}
    for spell in spells:
        for delegate in delegates:
            contract = delegate.vote_delegate_address
            if delegate.start_date > spell.start:
                status = NOT_STARTED
            elif sky_lookup.get((contract, spell.start), 0.0) == 0:
                status = NO_DELEGATED_SKY
            else:
                status = PENDING_VERIFICATION
            statuses[contract, spell.id] = status
    return statuses
