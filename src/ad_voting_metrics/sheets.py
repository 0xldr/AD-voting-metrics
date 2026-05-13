"""Google Sheets connection for reading/writing the AD compensation workbook.

This module provides the auth scaffolding only — opening a Spreadsheet object
the rest of the codebase can use. Tab creation, reading, and writing live in
their own modules layered on top.

Auth is via a Google Cloud service account, with credentials in a JSON key
file referenced by the GOOGLE_SERVICE_ACCOUNT_FILE env var. The workbook to
open is identified by SHEETS_WORKBOOK_ID. Both env vars are required; the
operator gets a clear error message naming the missing variable and pointing
at .env.example if either is unset, matching the existing DUNE_API_KEY
pattern.

The service account email (visible in the JSON key file as `client_email`)
must be added as an Editor on the target workbook. This is a one-time setup
step done in the Google Sheets sharing dialog; the script can't share itself
into a workbook.

Note on gspread: the library's maintainer announced in 2024 that they're
stepping back and looking for new maintainers. The library still works
against the current Google Sheets API v4. We'll revisit if maintenance
lapses become a real problem; for now it's the standard choice.
"""

import os
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

from .period import MonthPeriod

# Scopes required to read/write spreadsheets and open them by ID. The Drive
# scope is needed because gspread uses Drive APIs for open_by key.
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


def get_workbook(
    service_account_file: str | Path | None = None,
    workbook_id: str | None = None,
) -> gspread.Spreadsheet:
    """Open the configured workbook using the service account credentials.

    If service_account_file is None, reads GOOGLE_SERVICE_ACCOUNT_FILE from
    the environment. Similarly for workbook_id and SHEETS_WORKBOOK_ID. Pass
    explicit values for testing or one-off scripts that want to open a
    different workbook.

    Raises RuntimeError with a clear message for any of:
      - Missing env var (when the corresponding arg is var)
      - Service account file path doesn't exist or isn't readable
      - JSON key file is malformed or wrong format
      - Workbook ID is wrong, or the workbook isn't shared with the
      service account's email

    The error messages name the specific failure mode and point at the fix.
    """
    if service_account_file is None:
        service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not service_account_file:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_FILE environment variable is not set. "
                "Add it to your .env file (see .env.example), pointing at the "
                "JSON key file you download from Google Cloud Console."
            )

    if workbook_id is None:
        workbook_id = os.environ.get("SHEETS_WORKBOOK_ID")
        if not workbook_id:
            raise RuntimeError(
                "SHEETS_WORKBOOK_ID environment variable is not set. "
                "Add it to your .env file (see .env.example). The ID is the "
                "long string in the workbook URL between /d/ and /edit."
            )

    sa_path = Path(service_account_file)
    if not sa_path.exists():
        raise RuntimeError(
            f"Service account file not found at {sa_path}. "
            f"Check GOOGLE_SERVICE_ACCOUNT_FILE in your .env file points at "
            f"the correct path."
        )
    if not sa_path.is_file():
        raise RuntimeError(f"Service account file path {sa_path} exists but is not a file.")

    try:
        credentials = Credentials.from_service_account_file(
            str(sa_path),
            scopes=list(SCOPES),
        )
    except ValueError as e:
        # google-auth raises ValueError for malformed JSON or wrong key
        # file type (e.g., user credentials when service-account expected).
        raise RuntimeError(
            f"Service account file at {sa_path} could not be parsed as a "
            f"service-account JSON key. Original key: {e}"
        ) from e

    client = gspread.authorize(credentials)

    try:
        return client.open_by_key(workbook_id)
    except gspread.exceptions.APIError as e:
        # The most common failure here is the workbook not being shared
        # with the service account, which returns 403. We don't try to
        # parse the error structure exhaustively - just surface enough
        # context for the operator to debug.
        sa_email = credentials.service_account_email
        raise RuntimeError(
            f"Could not open workbook {workbook_id!r}. Most likely the "
            f"workbook isn't shared with the service account email "
            f"{sa_email}, or the workbook ID is wrong. Share the workbook "
            f"with that email as Editor in Google Sheets, then re-run. "
            f"Original error: {e}"
        ) from e


# ------------------------------------------------------------
# Tab management - list, create-or-get, clear.
# ------------------------------------------------------------


def list_tab_names(workbook: gspread.Spreadsheet) -> list[str]:
    """Return the title of every worksheet/tab in the workbook, in order.

    Thin wrapper over gspread; exists so callers can ask "does this tab
    exist?" without fishing through the gspread API directly, and so we
    have a single point to add caching or logging later if needed.
    """
    return [ws.title for ws in workbook.worksheets()]


def get_or_create_tab(
    workbook: gspread.Spreadsheet,
    title: str,
    *,
    rows: int,
    cols: int,
) -> gspread.Worksheet:
    """
    Return the worksheet with the given title, creating it if absent.

    rows and cols are required (keyword-only) - callers should size tabs
    based on the data they're about to write, not on arbitrary defaults.
    They apply only when creating a new tab; existing tabs keep their
    current dimensions regardless of what's passed. Resizing on every
    call would shrink/grow tabs unpredictably and risk losing operator-
    set sizing.

    Title matching is exact and case-sensitive (the underlying gspread
    behavior). Google Sheets allows tab titles up to 100 chars and forbids
    `[`, `]`, `*`, `?`, `:`, `/`, `\\`.
    """
    try:
        return workbook.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return workbook.add_worksheet(title=title, rows=rows, cols=cols)


def clear_tab(worksheet: gspread.Worksheet) -> None:
    """Wipe all cell values in the worksheet, leaving formatting intact.

    Uses gspread's worksheet.clear(), which clears cell values but
    preserves column widths, frozen rows, conditional formatting, and
    other operator-set sheet formatting. This is the right level for
    our re-write workflow — operators set up formatting once and
    re-runs preserve it. A "factory reset" that wipes formatting too
    would need a separate function.
    """
    worksheet.clear()


# ---------------------------------------------------
# Writers - populate workbook tabs from script data
# ---------------------------------------------------


# Daily Data tab schema. The columns mirror the existing df_ranking
# dataframe shape so the writer can copy values straight through without
# reshaping. Stored as a tuple so it's pinned and testable.
DAILY_DATA_COLUMNS: tuple[str, ...] = ("Date", "Delegate", "Total Delegation", "Rank")


def _daily_data_tab_title(period: MonthPeriod) -> str:
    """Tab title for a month's Daily Data, e.g. 'Daily Data April 2026."""
    return f"Daily Data {period}"


def write_daily_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df_ranking: pd.DataFrame,
) -> gspread.Worksheet:
    """Write the Daily Data tab for a period from the ranking dataframe."""
    missing = [c for c in DAILY_DATA_COLUMNS if c not in df_ranking.columns]
    if missing:
        raise ValueError(
            f"df_ranking is missing required columns: {missing}. Has: {list(df_ranking.columns)}"
        )

    subset = df_ranking[list(DAILY_DATA_COLUMNS)].copy()
    subset["Date"] = subset["Date"].apply(
        lambda d: "" if pd.isna(d) else (d.isoformat() if hasattr(d, "isoformat") else str(d))
    )

    header = list(DAILY_DATA_COLUMNS)
    rows = [
        [row["Date"], row["Delegate"], float(row["Total Delegation"]), int(row["Rank"])]
        for _, row in subset.iterrows()
    ]
    values = [header, *rows]

    title = _daily_data_tab_title(period)
    worksheet = get_or_create_tab(
        workbook,
        title,
        rows=max(len(values) + 10, 100),
        cols=max(len(header), 4),
    )

    clear_tab(worksheet)

    end_col_letter = _col_to_a1(len(header))
    range_name = f"A1:{end_col_letter}{len(values)}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


def _col_to_a1(col_index: int) -> str:
    if col_index < 1:
        raise ValueError(f"col_index must be >= 1, got {col_index}")
    result = ""
    n = col_index
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result
