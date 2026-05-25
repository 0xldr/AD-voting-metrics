"""Dune client: daily SKY delegation balances per vote-delegate contract.

Public entry points:
  - get_all_sky_delegated: fetch the underlying daily-balances table
  - get_delegate_list_sky: project that table onto the (delegate, day) grid for a period, zero-filling missing days

Also exposes `build_sky_lookup`, a helper used by the Sky vote-status modules to turn a per-day balance DataFrame into
an O(1) (contract, date) dict.
"""

import logging
import os
from datetime import date

import pandas as pd
from dune_client.client import DuneClient
from dune_client.query import QueryBase

from ad_voting_metrics.period import MonthPeriod

logger = logging.getLogger(__name__)

DUNE_SKY_QUERY_ID = 6604139


def get_all_sky_delegated(cache_max_age_hours: int | None = None) -> pd.DataFrame:
    """Fetch daily SKY delegations from Dune, indexed on (contract, date).

    With cache_max_age_hours set, uses Dune's get_latest_result and reuses  a cached execution if it's within the
    threshold; otherwise executes fresh. Useful during development to avoid burning Dune credits.

    Returns:
        DataFrame indexed on (delegation_contract, dt) with one running_total_balance column. delegation_contract is
        lowercased; dt is a datetime.date.

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
        DataFrame with columns: contract, name, date, sky. One row per (delegate, day) covering every day in the period.
    """
    all_sky_delegated = get_all_sky_delegated(cache_max_age_hours=cache_max_age_hours)

    days = list(pd.date_range(period.start, period.end, freq="D").date)
    contracts = df["Delegate Contract"].tolist()
    names_by_contract = {
        contract: name.strip().lower()
        for contract, name in zip(df["Delegate Contract"], df["Delegate Name"], strict=True)
    }

    target = pd.MultiIndex.from_product([contracts, days], names=["delegation_contract", "dt"])
    filled = all_sky_delegated["running_total_balance"].reindex(target, fill_value=0.0).astype(float).reset_index()

    return pd.DataFrame({
        "contract": filled["delegation_contract"],
        "name": filled["delegation_contract"].map(names_by_contract),
        "date": filled["dt"],
        "sky": filled["running_total_balance"],
    })


def build_sky_lookup(df_sky: pd.DataFrame) -> dict[tuple[str, date], float]:
    """Materialize df_sky into a (contract, date) -> sky-balance dict for O(1) lookup.

    Returns:
        Mapping of (contract, date) to that day's SKY balance.
    """
    return {(r["contract"], r["date"]): float(r["sky"]) for r in df_sky.to_dict(orient="records")}
