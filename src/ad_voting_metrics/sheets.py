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
from datetime import date, datetime
from pathlib import Path

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from .metrics import DISCOUNTED, NOT_PARTICIPATED, PARTICIPATED
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


# Daily Data tab schema. Long format, one row per (date, delegate).
# Workbook-wide tab: accumulates rows across all months ever fetched.
# The dataframe shape mirrors df_ranking from sky_dao so values copy
# through without reshaping
DAILY_DATA_COLUMNS: tuple[str, ...] = ("Date", "Delegate", "Total Delegation", "Rank")

DAILY_DATA_TAB_TITLE = "Daily Data"


def _read_daily_data_existing(
    worksheet: gspread.Worksheet,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Read the existing Daily Data tab into a dict keyed at (date, delegate).

    Returns a mapping from (date_iso_str, delegate_name) to (sky, rank).
    Empty worksheets (no header or no data) return an empty dict - the caller
    treats a missing/empty tab the same as a fresh start.
    """
    all_rows = worksheet.get_all_values()
    if not all_rows or len(all_rows) < 2:
        return {}

    header = all_rows[0]
    if header != list(DAILY_DATA_COLUMNS):
        return {}

    existing: dict[tuple[str, str], tuple[float, int]] = {}
    for row in all_rows[1:]:
        if len(row) < 4:
            continue
        date_str, delegate, sky_str, rank_str = row[0], row[1], row[2], row[3]
        if not date_str or not delegate:
            continue
        try:
            sky = float(sky_str)
            rank = int(rank_str)
        except (ValueError, TypeError):
            continue
        existing[(date_str, delegate)] = (sky, rank)

    return existing


def write_daily_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df_ranking: pd.DataFrame,
) -> gspread.Worksheet:
    """Write the workbook-wide Daily Data tab, merging in the current fetch."""
    missing = [c for c in DAILY_DATA_COLUMNS if c not in df_ranking.columns]
    if missing:
        raise ValueError(
            f"df_ranking is missing required columns: {missing}. Has: {list(df_ranking.columns)}"
        )

    subset = df_ranking[list(DAILY_DATA_COLUMNS)].copy()
    subset["Date"] = subset["Date"].apply(_coerce_date)

    new_rows: dict[tuple[str, str], tuple[float, int]] = {}
    new_counts_by_date: dict[str, int] = {}
    for _, row in subset.iterrows():
        date_str = str(row["Date"])
        delegate = str(row["Delegate"])
        sky = float(row["Total Delegation"])
        rank = int(row["Rank"])
        new_rows[(date_str, delegate)] = (sky, rank)
        new_counts_by_date[date_str] = new_counts_by_date.get(date_str, 0) + 1

    # Get-or-create the workbook-wide tab. Size generously; clear-and-rewrite
    # at the end takes care of the actual cell count.
    worksheet = get_or_create_tab(
        workbook,
        DAILY_DATA_TAB_TITLE,
        rows=max(len(new_rows) + 100, 200),
        cols=max(len(DAILY_DATA_COLUMNS), 4),
    )

    existing = _read_daily_data_existing(worksheet)

    # Roster drift check: dates present in both existing and new data
    # should have the same delegate count.
    existing_counts_by_date: dict[str, int] = {}
    for date_str, _delegate in existing:
        existing_counts_by_date[date_str] = existing_counts_by_date.get(date_str, 0) + 1

    for date_str, new_count in new_counts_by_date.items():
        if date_str in existing_counts_by_date:
            existing_count = existing_counts_by_date[date_str]
            if existing_count != new_count:
                raise ValueError(
                    f"Roster drift detected for date {date_str}: existing Daily Data "
                    f"has {existing_count} delegate rows, current fetch for {period} "
                    f"has {new_count}. The active-delegate count for that date "
                    f"changed between fetches. Reconcile manually (roster YAML) "
                    f"vs Communication Master / Daily Data) before re-running."
                )
    # Merge: existing rows for dates NOT in the current fetch stay; rows for
    # dates IN the current fetch get overwritten by new_rows values.
    dates_in_new = set(new_counts_by_date.keys())
    merged: dict[tuple[str, str], tuple[float, int]] = {}
    for key, value in existing.items():
        date_str, _delegate = key
        if date_str not in dates_in_new:
            merged[key] = value
    merged.update(new_rows)

    # Sort merged rows: (Date ascending, Rank ascending). Chronological with
    # rank ordering within each day.
    sorted_keys = sorted(
        merged.keys(),
        key=lambda k: (k[0], merged[k][1]),  # (date_str, rank)
    )

    header: list[object] = list(DAILY_DATA_COLUMNS)
    rows: list[list[object]] = []
    for key in sorted_keys:
        date_str, delegate = key
        sky, rank = merged[key]
        rows.append([date_str, delegate, sky, rank])
    values: list[list[object]] = [header, *rows]

    clear_tab(worksheet)

    # Compute the A1-style range explicitly so gspread doesn't have to
    # guess from values shape. The end cell is row=len(values), col=len(header).
    end_cell = rowcol_to_a1(len(values), len(header))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


# Participation Raw Data tab. Wide format: one row per poll/spell, columns
# for poll metadata then one column per delegate with their status.
# Fixed metadata columns; delegate columns are dynamic per period.
PARTICIPATION_METADATA_COLUMNS: tuple[str, ...] = (
    "Poll Id",
    "Start Date",
    "End Date",
    "Title",
)


def _participation_raw_data_tab_title(period: MonthPeriod) -> str:
    """Tab title for a month's Participation Raw Data."""
    return f"Participation Raw Data {period}"


def _lookup_poll_or_spell(
    identifier: str,
    poll_info: list[dict],
    spell_info: list[dict],
) -> dict | None:
    """Find a poll or spell record by ID/address. Returns None if not found.

    poll_info entries are keyed by `pollId`: spell_info by `address`.
    Identifiers are compared as strings to avoid type-mismatch surprises
    (poll IDs to come back as ints from some APIs, strs from others).
    """
    for poll in poll_info:
        if str(poll["pollId"]) == identifier:
            return poll
    for spell in spell_info:
        if str(spell["address"]) == identifier:
            return spell
    return None


def _coerce_date(value: date | datetime | None) -> str:
    """Convert a date/datetime/Timestamp/str to ISO date string.

    Used for the Start Date / End Date columns. Date-only ISO format
    for date objects; for datetimes and pandas
    Timestamps (which subclass datetime), take just the date portion.
    None becomes empty string.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        # datetime (and pd.Timestamp, which subclasses datetime) — take date portion
        return value.date().isoformat()
    return value.isoformat()


def write_participation_raw_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> gspread.Worksheet:
    """Write the Participation Raw Data tab for a period.

    Layout: one row per poll/spell with metadata + per-delegate status.
    Columns:
      Poll Id | Start Date | End Date | Title | <Delegate 1> | <Delegate 2> | ...

    Input shape (df, pre-`custom_sort`):
      Rows: one per delegate
      Columns: 'Delegate Name', 'Delegate Contract', 'Start Date'
               (delegate alignment start; unused here), then one column
               per poll_id (str) and one per spell address, each
               containing the per-delegate status for that poll/spell.

    The writer transposes the per-poll/spell columns into rows, joins
    metadata from poll_info and spell_info to fill Start Date / End Date /
    Title, and uses delegate names as column headers (delegate order
    preserved from df, which matches the YAML/roster order).

    Tab is named "Participation Raw Data {period}". Re-runs overwrite the
    tab idempotently via clear_tab + update.

    A poll/spell column in df with no matching entry in poll_info or
    spell_info gets blank metadata cells but the status column is still
    written — defensive, so a transient API inconsistency doesn't drop
    the participation data.
    """
    # Pull out delegate metadata. After this, the remaining columns are
    # all poll/spell IDs (whatever the upstream functions added).
    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [c for c in df.columns if c not in fixed_cols]

    if not poll_columns:
        # No polls or spells this month. Write header only, no data rows.
        # Rare but possible (zero-poll month). Operator sees empty table
        # rather than a confusing error.
        header = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]
        values: list[list[object]] = [header]
    else:
        # Header: metadata columns + one column per delegate (by name)
        header_list: list[object] = [
            *PARTICIPATION_METADATA_COLUMNS,
            *delegate_names,
        ]

        # Build data rows: one per poll/spell column
        rows: list[list[object]] = []
        for poll_id in poll_columns:
            metadata = _lookup_poll_or_spell(str(poll_id), poll_info, spell_info)
            if metadata is None:
                start_date_iso = ""
                end_date_iso = ""
                title = ""
            else:
                start_date_iso = _coerce_date(metadata.get("startDate"))
                end_date_iso = _coerce_date(metadata.get("endDate"))
                title = metadata.get("title", "")

            statuses = df[poll_id].tolist()

            row: list[object] = [
                str(poll_id),
                start_date_iso,
                end_date_iso,
                title,
                *statuses,
            ]
            rows.append(row)

        values = [header_list, *rows]

    title = _participation_raw_data_tab_title(period)
    worksheet = get_or_create_tab(
        workbook,
        title,
        rows=max(len(values) + 10, 100),
        cols=max(len(values[0]), len(PARTICIPATION_METADATA_COLUMNS) + 1),
    )

    clear_tab(worksheet)

    end_cell = rowcol_to_a1(len(values), len(values[0]))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


COMMUNICATION_MASTER_TAB_TITLE = "Communication Master"

# Default value for cells where the operator needs to review.
COMMUNICATION_PENDING_DEFAULT = "Pending verification"


def _isblank(value: str | None) -> bool:
    """Treat empty/None/whitespace-only as blank - operator-set cells have
    real content. Matches the spreadsheet notion of 'blank' for the
    fill-on-write logic.
    """
    if value is None:
        return True
    return not value.strip()


def _read_communication_master_existing(
    worksheet: gspread.Worksheet,
) -> tuple[list[str], dict[str, list[str]]]:
    """Read the existing Communication Master tab.

    Returns (header, rows_by_poll_id) where:
        - header is the list of column names exactly as found in row 1
        - rows_by_poll_id maps poll_id (from column A) to the row's cell
        values, padded/truncated to header length.

    Empty worksheets return an empty header and empty dict - the caller
    treats this as a fresh start.

    Skips rows where the Poll Id (column A) is blank.
    """
    all_rows = worksheet.get_all_values()
    if not all_rows or len(all_rows) < 1:
        return [], {}

    header = all_rows[0]
    if not header:
        return [], {}

    rows_by_poll_id: dict[str, list[str]] = {}
    n_cols = len(header)
    for row in all_rows[1:]:
        if not row or not row[0].strip():
            continue
        poll_id = row[0]
        # Pad short rows so every entry has n_cols cells; truncate long ones
        if len(row) < n_cols:
            normalized_row = row + [""] * (n_cols - len(row))
        elif len(row) > n_cols:
            normalized_row = row[:n_cols]
        else:
            normalized_row = row
        rows_by_poll_id[poll_id] = normalized_row

    return header, rows_by_poll_id


def write_communication_master(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> gspread.Worksheet:
    """Write the workbook-wide Communication Master tab.

    Layout matches Participation Raw Data:
    """
    if "Delegate Name" not in df.columns:
        raise ValueError("df must have a 'Delegate Name' columns")

    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [c for c in df.columns if c not in fixed_cols]

    # Get-or-create the workbook-wide tab.
    worksheet = get_or_create_tab(
        workbook,
        COMMUNICATION_MASTER_TAB_TITLE,
        rows=max(len(poll_columns) + 100, 200),
        cols=max(len(PARTICIPATION_METADATA_COLUMNS) + len(delegate_names), 10),
    )

    existing_header, existing_rows = _read_communication_master_existing(worksheet)

    # Determine the output header. First fetch (empty tab): header is
    # metadata + current roster delegates. Subsequent fetches: preserve
    # existing header (column order is operator-visible) and validate
    # that every active delegate has a column
    if not existing_header:
        header: list[str] = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]
    else:
        header = list(existing_header)
        # Fatal check: every active-roster delegate must be in the header.
        # Delegates in the header but not in df are fine (historical).
        existing_columns_set = set(existing_header)
        missing = [n for n in delegate_names if n not in existing_columns_set]
        if missing:
            raise ValueError(
                f"Communication Master is missing column(s) for delegate(s): "
                f"{missing}. Add a column header with the exact delegate name "
                f"for each missing delegate to the Communication Master tab, "
                f"then re-run. (Columns can't be auto-added - operators control "
                f"column placement and naming.)"
            )

    # Build column_index: where each delegate's column is in the header.
    # Metadata columns occupy positions 0..3.
    n_metadata = len(PARTICIPATION_METADATA_COLUMNS)
    delegate_col_index: dict[str, int] = {col: i for i, col in enumerate(header) if i >= n_metadata}

    # Index df rows by delegate name -> row Series (for participation lookups).
    df_by_delegate: dict[str, pd.Series] = {
        str(row["Delegate Name"]): row for _, row in df.iterrows()
    }
    current_roster = set(df_by_delegate.keys())

    # Build merged rows by poll id. For each poll/spell column in df:
    #   - if poll already in existing rows: take existing row, fill blanks
    #   - if poll is new: build a fresh row with pending/cross-ref defaults
    # Plus: preserve existing rows for polls not in current df (historical).
    merged_rows: dict[str, list[str]] = {}

    for poll_id, row in existing_rows.items():
        padded_row = row + [""] * (len(header) - len(row)) if len(row) < len(header) else row
        merged_rows[poll_id] = list(padded_row)

    for poll_id in poll_columns:
        poll_id_str = str(poll_id)
        metadata = _lookup_poll_or_spell(poll_id_str, poll_info, spell_info)
        if metadata is None:
            start_date_iso = ""
            end_date_iso = ""
            title = ""
        else:
            start_date_iso = _coerce_date(metadata.get("startDate"))
            end_date_iso = _coerce_date(metadata.get("endDate"))
            title = str(metadata.get("title", ""))

        participation_per_col: list[str] = [""] * len(header)
        for col_name, col_idx in delegate_col_index.items():
            # Active roster delegate - look up their participation status
            # for this poll. df has the poll_id as a column with per-row
            # delegate statuses
            if col_name in current_roster:
                p_status = str(df_by_delegate[col_name].get(poll_id, ""))
                participation_per_col[col_idx] = p_status
            # else: not in current roster + leave participation blank
            # default will also be blank

        default_comm_per_col: list[str] = [""] * len(header)
        for col_name, col_idx in delegate_col_index.items():
            if col_name not in current_roster:
                continue
            p = participation_per_col[col_idx]
            if p in NOT_PARTICIPATED:
                default_comm_per_col[col_idx] = "Did not vote"
            elif p in DISCOUNTED:
                default_comm_per_col[col_idx] = p
            elif p in PARTICIPATED:
                default_comm_per_col[col_idx] = COMMUNICATION_PENDING_DEFAULT
            else:
                default_comm_per_col[col_idx] = ""

        if poll_id_str in merged_rows:
            row = merged_rows[poll_id_str]
            for i, current_val in enumerate([poll_id_str, start_date_iso, end_date_iso, title]):
                if _isblank(row[i]):
                    row[i] = current_val
            # Fill delegate-column blanks with the default:
            for col_idx in delegate_col_index.values():
                if _isblank(row[col_idx]):
                    row[col_idx] = default_comm_per_col[col_idx]
            merged_rows[poll_id_str] = row
        else:
            row = [""] * len(header)
            row[0] = poll_id_str
            row[1] = start_date_iso
            row[2] = end_date_iso
            row[3] = title
            for col_idx in delegate_col_index.values():
                row[col_idx] = default_comm_per_col[col_idx]
            merged_rows[poll_id_str] = row

    def _sort_key(item: tuple[str, list[str]]) -> tuple[int, str]:
        _, row = item
        start = row[1] if len(row) > 1 else ""
        try:
            datetime.fromisoformat(start.replace("Z", "+00:00"))
            return (0, start)
        except ValueError:
            return (1, "")

    items = list(merged_rows.items())
    items.sort(
        key=_sort_key,
        reverse=False,
    )
    rank0 = [it for it in items if _sort_key(it)[0] == 0]
    rank1 = [it for it in items if _sort_key(it)[0] == 1]
    rank0.reverse()
    items = rank0 + rank1

    rows: list[list[object]] = [list(row) for _, row in items]
    values: list[list[object]] = [list(header), *rows]

    clear_tab(worksheet)

    end_cell = rowcol_to_a1(len(values), len(header))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet
