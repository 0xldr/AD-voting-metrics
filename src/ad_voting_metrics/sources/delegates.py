"""Client for the vote.sky.money delegates API.

Fetches the currently aligned delegate set. Paginates via paginationInfo.hasNextPage.
"""

import logging

from .http import HTTP_TIMEOUT, get_session

logger = logging.getLogger(__name__)

DELEGATES_URL = "https://vote.sky.money/api/delegates"
PAGE_SIZE = 20

# Defensive cap: bail out if the API returns hasNextPage=true forever
# With ~13 aligned delegates today and pageSize=20, one page suffices.
_MAX_PAGES = 50


def fetch_aligned_delegates() -> list[dict]:
    """Fetch every current aligned delegate from the API.

    Paginates until hasNextPage is false or _MAX_PAGES is hit.

    Returns:
        List of raw delegate dicts from the API response.
    """
    session = get_session()
    delegates: list[dict] = []

    for page in range(1, _MAX_PAGES + 1):
        params: dict[str, str | int] = {
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
        if not page_delegates:
            break

        delegates.extend(page_delegates)

        pagination = data.get("paginationInfo", {})
        if not pagination.get("hasNextPage"):
            break
    else:
        # for-else: fires only if the loop exhausts without breaking,
        # i.e. we hit the page cap with hasNextPage still true.
        logger.warning(
            "fetch_aligned_delegates hit page cap of %d with hasNextPage still true; results may be incomplete",
            _MAX_PAGES,
        )
    logger.info("Fetched %d aligned delegates from API", len(delegates))
    return delegates
