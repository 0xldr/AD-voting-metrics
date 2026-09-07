"""Client for the vote.sky.money delegates API: the currently aligned delegate set."""

import logging
from typing import Any

from .http import fetch_json, paginate

logger = logging.getLogger(__name__)

DELEGATES_URL = "https://vote.sky.money/api/delegates"
PAGE_SIZE = 20


def fetch_aligned_delegates() -> list[dict[str, Any]]:
    """Fetch every current aligned delegate from the API as raw response dicts."""

    def delegates_page(number: int) -> list[dict[str, Any]]:
        data = fetch_json(
            DELEGATES_URL,
            network="mainnet",
            pageSize=PAGE_SIZE,
            page=number,
            orderBy="DATE",
            orderDirection="DESC",
            delegateType="ALIGNED",
        )
        return list(data.get("delegates", []))

    delegates = [d for page in paginate(delegates_page) for d in page]
    logger.info("Fetched %d aligned delegates from vote.sky.money", len(delegates))
    return delegates
