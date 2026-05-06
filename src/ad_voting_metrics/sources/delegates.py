"""Client for the vote.sky.money delegates API.

Fetches the currently aligned delegate set. Paginates via paginationInfo.hasNextPage.
"""

import logging

from ..sky_dao import HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

DELEGATES_URL = "https://vote.sky.money/api/delegates"
PAGE_SIZE = 20

# Defensive cap: if the API returns hasNextPage=true forever (bug or misconfig),
# stop after this many pages rather than looping forever. With ~13 aligned
# delegates today and pageSize=20, one page suffices; we'd need 100x growth
# before this matters, and at that point the script needs other rethinking too.
_MAX_PAGES = 50


def fetch_aligned_delegates() -> list[dict]:
    """Fetch every current aligned delegate from the API, handling pagination.

    Returns the raw dicts as they appear in the API response.

    Pagination: requests pages of size PAGE_SIZE until paginationInfo.hasNextPage is false
    or we hit _MAX_PAGES.
    """
    session = get_session()
    delegates: list[dict] = []
    page = 1

    while page <= _MAX_PAGES:
        params = {
            "network": "mainnet",
            "pageSize": PAGE_SIZE,
            "page": page,
            "orderBy": "DATE",
            "orderDirection": "DESC",
            "delegateType": "ALIGNED",
        }
        response = session.get(DELEGATES_URL, params=params, timeout=HTTP_TIMEOUT)
        response.raise_for_status()
        data = response.json()

        page_delegates = data.get("delegates", [])
        delegates.extend(page_delegates)

        pagination = data.get("paginationInfo", {})
        if not pagination.get("hasNextPage"):
            break

        page += 1
    else:
        # while-else: only runs if the loop exhausts without breaking, i.e. we
        # hit the page cap with hasNextPage still true. Worth a loud warning.
        logger.warning(
            "fetch_aligned_delegates hit page cap of %d with hasNextPage still true; "
            "results may be incomplete",
            _MAX_PAGES,
        )
    logger.info("Fetched %d aligned delegates from API", len(delegates))
    return delegates
