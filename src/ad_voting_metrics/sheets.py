"""Google Sheets connection for reading/writing the AD compensation workbook.

Auth is via a Google Cloud service account; the JSON key file is referenced
by GOOGLE_SERVICE_ACCOUNT_FILE and the workbook by SHEETS_WORKBOOK_ID. The
service account's client_email must be added as Editor on the workbook -
a one-time setup step in the Google Sheets sharing dialog.
"""

import calendar
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

from .metrics import DISCOUNTED, NOT_PARTICIPATED, PARTICIPATED
from .period import MonthPeriod

if TYPE_CHECKING:
    from .compensation import CompensationConfig, PeriodCompensation

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

    If service_account_file or workbook_id is None, reads from the matching
    env var (GOOGLE_SERVICE_ACCOUNT_FILE / SHEETS WORKBOOK_ID). Pass explicit
    values for testing.

    Returns:
        The opened gspread.Spreadsheet.

    Raises:
        RuntimeError: for any of:
          - Missing env var (when the corresponding arg is None)
          - Service account file path doesn't exist or isn't readable
          - JSON key file is malformed or wrong format
          - Workbook ID is wrong, or not shared with the service account
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
        # google-auth raises ValueError for malformed JSON or wrong key type
        # (e.g., user credentials when service-account expected).
        raise RuntimeError(
            f"Service account file at {sa_path} could not be parsed as a "
            f"service-account JSON key. Original key: {e}"
        ) from e

    client = gspread.authorize(credentials)

    try:
        return client.open_by_key(workbook_id)
    except gspread.exceptions.APIError as e:
        # Usually a 403 because the workbook isn't shared with the service
        # account. Surface the email so the operator can fix it.
        sa_email = credentials.service_account_email
        raise RuntimeError(
            f"Could not open workbook {workbook_id!r}. Most likely the "
            f"workbook isn't shared with the service account email "
            f"{sa_email}, or the workbook ID is wrong. Share the workbook "
            f"with that email as Editor in Google Sheets, then re-run. "
            f"Original error: {e}"
        ) from e


def list_tab_names(workbook: gspread.Spreadsheet) -> list[str]:
    """Return the title of every worksheet/tab in the workbook, in order."""
    return [ws.title for ws in workbook.worksheets()]


def get_or_create_tab(
    workbook: gspread.Spreadsheet,
    title: str,
    *,
    rows: int,
    cols: int,
) -> gspread.Worksheet:
    r"""Return the worksheet with the given title, creating it if absent.

    `rows` and `cols` apply only on creation; existing tabs keep their
    current dimensions. They're keyword-only to force callers to size
    new tabs based on the data they're about to write.

    Title matching is exact and case-sensitive. Tab titles forbid
    `[`, `]`, `*`, `?`, `:`, `/`, `\` — not validated here; gspread
    will surface the API error.

    Returns:
        The worksheet.
    """
    try:
        return workbook.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return workbook.add_worksheet(title=title, rows=rows, cols=cols)


def clear_tab(worksheet: gspread.Worksheet) -> None:
    """Wipe all cell values in the worksheet, leaving formatting intact."""
    worksheet.clear()


def _open_required_tab(
    workbook: gspread.Spreadsheet,
    title: str,
    instructions: str,
) -> gspread.Worksheet:
    """Open the named tab or raise RuntimeError with an operator-friendly message.

    The raised message is "Workbook is missing the '<title>' tab. <instructions>"

    Returns:
        The worksheet.

    Raises:
        RuntimeError: if the tab doesn't exist.
    """
    try:
        return workbook.worksheet(title)
    except gspread.exceptions.WorksheetNotFound as exc:
        raise RuntimeError(f"Workbook is missing the '{title}' tab. {instructions}") from exc


# Daily Data tab: long format, one row per (date, delegate), workbook-wide.
DAILY_DATA_COLUMNS: tuple[str, ...] = ("Date", "Delegate", "Total Delegation", "Rank")

DAILY_DATA_TAB_TITLE = "Daily Data"


def _read_daily_data_existing(
    worksheet: gspread.Worksheet,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Read the existing Daily Data tab into a dict keyed at (date, delegate).

    Empty worksheets return an empty dict. Malformed rows are skipped
    rather than crashing - better to lose one row than the whole tab.

    Returns:
        Mapping from (date_iso_str, delegate_name) to (sky, rank).
    """
    all_rows = worksheet.get_all_values()
    if not all_rows or len(all_rows) < 2:
        return {}

    header = all_rows[0]
    if header != list(DAILY_DATA_COLUMNS):
        # Different shape (older version or operator-modified) - treat as
        # empty; the clear-and-rewrite below will replace it.
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
        existing[date_str, delegate] = (sky, rank)

    return existing


def write_daily_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df_ranking: pd.DataFrame,
) -> gspread.Worksheet:
    """Write the workbook-wide Daily Data tab, merging in the current fetch.

    Long format, one row per (date, delegate). Columns are exactly
    DAILY_DATA_COLUMNS; extras in df_ranking are ignored.

    Merge behavior:
      - Existing rows for dates not in the current fetch are preserved.
      - Existing rows for dates in the current fetch are overwritten
        (re-runs are idempotent).
      - New dates are added.

    For dates appearing in both existing and new data, the count of
    delegate rows must match - a mismatch indicates roster drift
    (delegate added/removed) and the function raises rather than
    silently shifting which delegates are represented.

    Date values are coerced to 'YYYY-MM-DD' ISO strings. Output is
    sorted by (Date asc, Rank asc).

    Tab name is "Daily Data" (no period suffix); `period` is retained
    for error messages.

    Returns:
        The worksheet that was written.

    Raises:
        ValueError: if df_ranking is missing required columns, or on
            roster drift.
    """
    missing = [c for c in DAILY_DATA_COLUMNS if c not in df_ranking.columns]
    if missing:
        raise ValueError(
            f"df_ranking is missing required columns: {missing}. Has: {list(df_ranking.columns)}"
        )

    subset = df_ranking[list(DAILY_DATA_COLUMNS)].copy()
    subset["Date"] = subset["Date"].apply(_coerce_date)

    new_rows: dict[tuple[str, str], tuple[float, int]] = {}
    new_counts_by_date: Counter[str] = Counter()
    for _, row in subset.iterrows():
        date_str = str(row["Date"])
        delegate = str(row["Delegate"])
        sky = float(row["Total Delegation"])
        rank = int(row["Rank"])
        new_rows[date_str, delegate] = (sky, rank)
        new_counts_by_date[date_str] += 1

    # Size generously; clear-and-rewrite at the end sets the actual cell count.
    worksheet = get_or_create_tab(
        workbook,
        DAILY_DATA_TAB_TITLE,
        rows=max(len(new_rows) + 100, 200),
        cols=max(len(DAILY_DATA_COLUMNS), 4),
    )

    existing = _read_daily_data_existing(worksheet)

    existing_counts_by_date: Counter[str] = Counter(date_str for (date_str, _delegate) in existing)

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
    # Merge: preserve existing dates not in the current fetch; overwrite
    # the rest with new values.
    dates_in_new = set(new_counts_by_date.keys())
    merged: dict[tuple[str, str], tuple[float, int]] = {}
    for key, value in existing.items():
        date_str, _delegate = key
        if date_str not in dates_in_new:
            merged[key] = value
    merged.update(new_rows)

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

    end_cell = rowcol_to_a1(len(values), len(header))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


# Participation Raw Data tab: wide format, one row per poll/spell. Fixed
# metadata columns followed by one column per delegate.
PARTICIPATION_METADATA_COLUMNS: tuple[str, ...] = (
    "Poll Id",
    "Start Date",
    "End Date",
    "Title",
)


def _participation_raw_data_tab_title(period: MonthPeriod) -> str:
    """Return the per-month tab title, e.g. "Participation Raw Data April 2026"."""
    return f"Participation Raw Data {period}"


def _lookup_poll_or_spell(
    identifier: str,
    poll_info: list[dict],
    spell_info: list[dict],
) -> dict | None:
    """Find a poll or spell record by ID/address; compare as strings.

    Returns:
        The matching record, or None if no match.
    """
    for poll in poll_info:
        if str(poll["pollId"]) == identifier:
            return poll
    for spell in spell_info:
        if str(spell["address"]) == identifier:
            return spell
    return None


def _coerce_date(value: date | datetime | None) -> str:
    """Convert a date/datetime/pd.Timestamp/None to a 'YYYY-MM-DD' string.

    None becomes empty string. Datetimes (and pd.Timestamp, which
    subclasses datetime) collapse to their date portion.

    Returns:
        The ISO date string, or empty string for None.
    """
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    return value.isoformat()


def build_participation_values(
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> list[list[object]]:
    """Build the 2D values matrix for the Participation Raw Data layout.

    Input df shape: one row per delegate, with fixed columns
    'Delegate Name', 'Delegate Contract', 'Start Date', then one column
    per poll_id (str) and one per spell address.

    Returns:
        A list-of-lists with header row first, then one row per poll/spell:
          [Poll Id, Start Date, End Date, Title, <Delegate 1 status>, ...]

        Zero-poll months return a header-only matrix (single row).

        Poll/spell columns with no matching poll_info or spell_info entry
        get blank metadata cells; the status column is still written so a
        transient API inconsistency can't drop participation data.

        Spell rows have blank End Date — spells don't carry endDate in
        spell_info.
    """
    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [c for c in df.columns if c not in fixed_cols]

    header: list[object] = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]
    if not poll_columns:
        return [header]

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
        rows.append([str(poll_id), start_date_iso, end_date_iso, title, *statuses])

    return [header, *rows]


def write_participation_raw_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> gspread.Worksheet:
    """Write the Participation Raw Data tab for a period.

    Layout: one row per poll/spell with metadata + per-delegate status.
    Columns: Poll Id | Start Date | End Date | Title | <Delegate 1> | ...

    Input df shape (pre-`custom_sort`): one row per delegate, with
    fixed columns 'Delegate Name', 'Delegate Contract', 'Start Date',
    then one column per poll_id(str) and one per spell address.

    Tab is named "Participation Raw Data {period}". Re-runs overwrite.

    Poll/spell columns in df with no matching poll_info or spell_info
    get blank metadata cells; the status column is still written
    so a transient API inconsistency can't drop participation data.

    Spell rows have blank End Date - spells don't carry endDate in
    spell_info.

    Returns:
        The worksheet that was written.
    """
    values = build_participation_values(df, poll_info, spell_info)

    tab_title = _participation_raw_data_tab_title(period)
    worksheet = get_or_create_tab(
        workbook,
        tab_title,
        rows=max(len(values) + 10, 100),
        cols=max(len(values[0]), len(PARTICIPATION_METADATA_COLUMNS) + 1),
    )

    clear_tab(worksheet)

    end_cell = rowcol_to_a1(len(values), len(values[0]))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


# Communication Master: workbook-wide tab matching Participation Raw Data's
# shape, with one column per delegate. Operators review cells manually;
# script-set defaults follow the participation cross-reference rule.
COMMUNICATION_MASTER_TAB_TITLE = "Communication Master"
COMMUNICATION_PENDING_DEFAULT = "Pending verification"


def _isblank(value: str | None) -> bool:
    """Return True for None, empty string, or whitespace-only - the spreadsheet sense of "blank"."""
    if value is None:
        return True
    return not value.strip()


def _read_communication_master_existing(
    worksheet: gspread.Worksheet,
) -> tuple[list[str], dict[str, list[str]]]:
    """Read the existing Communication Master tab.

    Empty worksheets return an empty header and empty dict - caller
    treats this as a fresh start. Rows with a blank Poll Id (column A)
    are skipped.

    Returns:
        (header, rows_by_poll_id) tuple where:
          - header 1 is row 1 verbatim
          - rows_by_poll_id maps poll_id to padded/truncated row values
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
        if len(row) < n_cols:
            normalized_row = row + [""] * (n_cols - len(row))
        elif len(row) > n_cols:
            normalized_row = row[:n_cols]
        else:
            normalized_row = row
        rows_by_poll_id[poll_id] = normalized_row

    return header, rows_by_poll_id


def _apply_cross_reference_rule(participation: str) -> str:
    """Return the default communication status for a given participation status."""
    if participation in NOT_PARTICIPATED:
        return "Did not vote"
    if participation in DISCOUNTED:
        return participation
    if participation in PARTICIPATED:
        return COMMUNICATION_PENDING_DEFAULT
    return ""


def _build_comm_row_for_poll(
    poll_id_str: str,
    start_date_iso: str,
    end_date_iso: str,
    title: str,
    header_len: int,
    delegate_col_index: dict[str, int],
    df_by_delegate: dict[str, pd.Series],
    current_roster: set[str],
    poll_id: object,
    existing_row: list[str] | None,
) -> list[str]:
    """Build one merged Communication Master row.

    For an existing row, blanks are filled with current metadata or
    defaults; non-blank cells (operator edits) are preserved. For a new
    row, metadata cells and per-delegate defaults are written; cells
    for delegates no longer in the roster stay blank.

    Returns:
        A row matching the current header length.
    """
    defaults: dict[int, str] = {}
    for col_name, col_idx in delegate_col_index.items():
        if col_name not in current_roster:
            continue
        p_status = str(df_by_delegate[col_name].get(poll_id, ""))
        defaults[col_idx] = _apply_cross_reference_rule(p_status)

    if existing_row is not None:
        row = list(existing_row)
        for i, current_val in enumerate([poll_id_str, start_date_iso, end_date_iso, title]):
            if _isblank(row[i]):
                row[i] = current_val
        for col_idx, default_val in defaults.items():
            row[col_idx] = default_val
        return row

    row = [""] * header_len
    row[0] = poll_id_str
    row[1] = start_date_iso
    row[2] = end_date_iso
    row[3] = title
    for col_idx, default_val in defaults.items():
        row[col_idx] = default_val
    return row


def _sort_comm_rows_by_start_date(merged_rows: dict[str, list[str]]) -> list[tuple[str, list[str]]]:
    """Sort rows by Start Date descending; unparseable/blank Start Date go to end.

    Returns:
        List of (poll_id, row) tuples in the final write order.
    """
    parseable: list[tuple[str, list[str]]] = []
    unparseable: list[tuple[str, list[str]]] = []
    for poll_id, row in merged_rows.items():
        start = row[1] if len(row) > 1 else ""
        try:
            datetime.fromisoformat(start.replace("Z", "+00:00"))
            parseable.append((poll_id, row))
        except ValueError:
            unparseable.append((poll_id, row))
    parseable.sort(key=lambda it: it[1][1], reverse=True)
    return parseable + unparseable


def write_communication_master(
    workbook: gspread.Spreadsheet,
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> gspread.Worksheet:
    """Write the workbook-wide Communication Master tab.

    Layout matches Participation Raw Data:
        Poll Id | Start Date | End Date | Title | <Delegate 1> | ...

    Workbook-wide (not per-month). Each fetch merges new polls/spells
    into the existing tab; operator edits to existing cells are
    preserved.

    Cell defaults (for blank or new cells):
      - In current roster: apply the cross-reference rule
        ("Yes" -> "Pending verification", "No" -> "Did not vote",
        DISCOUNTED -> mirror the participation status).
      - Not in current roster (column persists historically but the
        delegate is no longer in the YAML): leave blank.

    Every delegate in df must have a column in the existing tab - if a
    new delegate has been added to the YAML but not to the tab, the
    operator must add the column manually before re-running. Empty
    tabs are an exception (first write): columns are created from df.

    Rows are sorted by Start Date descending; rows with missing/
    unparseable Start Date sort to the end. Spell rows have blank End
    Date.

    Returns:
        The worksheet that was written.

    Raises:
        ValueError: if df is missing the 'Delegate Name' column, or if
            a delegate in df has no column in a non-empty existing tab.
    """
    if "Delegate Name" not in df.columns:
        raise ValueError("df must have a 'Delegate Name' columns")

    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [c for c in df.columns if c not in fixed_cols]

    worksheet = get_or_create_tab(
        workbook,
        COMMUNICATION_MASTER_TAB_TITLE,
        rows=max(len(poll_columns) + 100, 200),
        cols=max(len(PARTICIPATION_METADATA_COLUMNS) + len(delegate_names), 10),
    )

    existing_header, existing_rows = _read_communication_master_existing(worksheet)

    if not existing_header:
        header: list[str] = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]
    else:
        header = list(existing_header)
        missing = [n for n in delegate_names if n not in set(existing_header)]
        if missing:
            raise ValueError(
                f"Communication Master is missing column(s) for delegate(s): "
                f"{missing}. Add a column header with the exact delegate name "
                f"for each missing delegate to the Communication Master tab, "
                f"then re-run. (Columns can't be auto-added - operators control "
                f"column placement and naming.)"
            )

    n_metadata = len(PARTICIPATION_METADATA_COLUMNS)
    delegate_col_index: dict[str, int] = {col: i for i, col in enumerate(header) if i >= n_metadata}

    df_by_delegate: dict[str, pd.Series] = {str(row["Delegate Name"]): row for _, row in df.iterrows()}
    current_roster = set(df_by_delegate.keys())

    # Seed with existing rows, padding to current header length so an
    # operator-added column gets blanks for old polls.
    merged_rows: dict[str, list[str]] = {
        poll_id: list(row) + [""] * max(0, len(header) - len(row)) for poll_id, row in existing_rows.items()
    }

    for poll_id in poll_columns:
        poll_id_str = str(poll_id)
        metadata = _lookup_poll_or_spell(poll_id_str, poll_info, spell_info)

        start_date_iso = _coerce_date(metadata.get("startDate")) if metadata else ""
        end_date_iso = _coerce_date(metadata.get("endDate")) if metadata else ""
        title = str(metadata.get("title", "")) if metadata else ""

        merged_rows[poll_id_str] = _build_comm_row_for_poll(
            poll_id_str=poll_id_str,
            start_date_iso=start_date_iso,
            end_date_iso=end_date_iso,
            title=title,
            header_len=len(header),
            delegate_col_index=delegate_col_index,
            df_by_delegate=df_by_delegate,
            current_roster=current_roster,
            poll_id=poll_id,
            existing_row=merged_rows.get(poll_id_str),
        )

    sorted_items = _sort_comm_rows_by_start_date(merged_rows)
    rows: list[list[object]] = [list(row) for _, row in sorted_items]
    values: list[list[object]] = [list(header), *rows]

    clear_tab(worksheet)

    end_cell = rowcol_to_a1(len(values), len(header))
    range_name = f"A1:{end_cell}"
    worksheet.update(values=values, range_name=range_name)

    return worksheet


# ---------------------------------------------------------------------------
# Config tab (workbook-wide) and Compensation tab (per-period)
# ---------------------------------------------------------------------------


CONFIG_TAB_TITLE = "Config"

_REQUIRED_CONFIG_KEYS = ("L1_USDS", "L2_USDS", "L3_USDS", "TOTAL_SLOTS")

COMPENSATION_COLUMNS = (
    "Delegate",
    "Participation 6-month %",
    "Communication 6-month %",
    "Metrics Modifier",
    "Ranked During Month?",
    "Days As Ranked",
    "Entitlement Pre-Modifiers (USDS)",
    "Final Amount to AD Buffer (USDS)",
    "Rank at Month End",
    "Amount in Buffer at Month Start (USDS)",
    "Amount Added to AD Buffer (USDS)",
    "Payment Amount (USDS)",
    "Scaled Buffer Contents Post Payment (USDS)",
    "Notes",
)


def _compensation_tab_title(period: MonthPeriod) -> str:
    """Return the per-period tab title, e.g. "April 2026 Compensation"."""
    return f"{period} Compensation"


def read_config(workbook: gspread.Spreadsheet) -> "CompensationConfig":
    """Read the workbook-wide Config tab and return a CompensationConfig.

    Format: two columns (Key, Value), header in row 1. Required keys:
    L1_USDS, L2_USDS, L3_USDS, TOTAL_SLOTS. Unknown keys are ignored.

    Returns:
        Parsed CompensationConfig.

    Raises:
        RuntimeError: if the Config tab doesn't exist.
        ValueError: if a required key is missing or a value can't
            be coerced.
    """
    worksheet = _open_required_tab(
        workbook,
        CONFIG_TAB_TITLE,
        f"Create it with columns Key and Value, and rows for: {', '.join(_REQUIRED_CONFIG_KEYS)}",
    )

    rows = worksheet.get_all_values()
    if not rows:
        raise ValueError(f"'{CONFIG_TAB_TITLE}' tab is empty.")

    # Skip the header rows; collect Key -> raw Value strings
    kv: dict[str, str] = {}
    for row in rows[1:]:
        if len(row) < 2:
            continue
        key, value = row[0].strip(), row[1].strip()
        if not key:
            continue
        kv[key] = value

    missing = [k for k in _REQUIRED_CONFIG_KEYS if k not in kv]
    if missing:
        raise ValueError(
            f"'{CONFIG_TAB_TITLE}' tab is missing required keys: {missing}. "
            f"Required: {list(_REQUIRED_CONFIG_KEYS)}."
        )

    try:
        l1 = float(kv["L1_USDS"])
        l2 = float(kv["L2_USDS"])
        l3 = float(kv["L3_USDS"])
        total = int(kv["TOTAL_SLOTS"])
    except ValueError as exc:
        raise ValueError(f"'{CONFIG_TAB_TITLE}' has un-parseable value: {exc}") from exc

    from .compensation import CompensationConfig

    return CompensationConfig(
        l1_usds=l1,
        l2_usds=l2,
        l3_usds=l3,
        total_slots=total,
    )


def _level_label(level: int | None) -> str:
    """Map an assigned_level (1/2/3/None) to the workbook's H-column label.

    Returns:
        "Level 1", "Level 2", "Level 3", or "No".
    """
    if level == 1:
        return "Level 1"
    if level == 2:
        return "Level 2"
    if level == 3:
        return "Level 3"
    return "No"


def _format_pct(pct: float | None) -> str | float:
    """Format a fractional pct for the Compensation tab.

    Returns:
        The float unchanged for numberic values, or "No Data" for None.
    """
    if pct is None:
        return "No Data"
    return pct


def write_compensation_tab(
    workbook: gspread.Spreadsheet,
    period_comp: "PeriodCompensation",
) -> gspread.Worksheet:
    """Write the per-period Compensation tab. Re-runs overwrite.

    Tab name: "{Month Year} Compensation".

    Layout:
      - Rows 1-5: period metadata (Year, Month, Period Start/End, Days)
      - Rows 1-3 cols D-E: L1/L2/L3 USDS reference values from config.
      - Rows 1-3 cols G-H: counts of delegates at each level.
      - Row 7: Total Final Amount label + sum formula.
      - Row 8: Slot Days Check GOOD/NOT GOOD status.
      - Row 9: column headers (14 columns A-N).
      - Rows 10+: one row per delegate, alphabetical.

    Returns:
        The worksheet that was written.
    """
    rows_in = period_comp.per_delegate

    # Count delegates at each level (based on level_at_period_end).
    n_l1 = sum(1 for r in rows_in if r.level_at_period_end == 1)
    n_l2 = sum(1 for r in rows_in if r.level_at_period_end == 2)
    n_l3 = sum(1 for r in rows_in if r.level_at_period_end == 3)

    period = period_comp.period
    config = period_comp.config

    n_data_rows = len(rows_in)
    last_data_row = 9 + n_data_rows  # row 9 = header, rows 10..(9+n) = data
    sum_formula = f"=SUM(H10:H{max(last_data_row, 10)})"

    # Build header block as full rows so we can write it with one update
    # Rows are 1-indexed in Sheets; we'll pad to row 9 for the headers
    header_block: list[list[object]] = [
        # Row 1
        ["Year", period.year, "", "Level 1 USDS", config.l1_usds, "", "Number of Level 1", n_l1],
        # Row 2
        [
            "Month",
            calendar.month_name[period.month],
            "",
            "Level 2 USDS",
            config.l2_usds,
            "",
            "Number of Level 2",
            n_l2,
        ],
        # Row 3
        [
            "Period Start",
            _coerce_date(period.start),
            "",
            "Level 3 USDS",
            config.l3_usds,
            "",
            "Number of Level 3",
            n_l3,
        ],
        # Row 4
        ["Period End", _coerce_date(period.end), "", "", "", "", "", ""],
        # Row 5
        ["Days in Month", period_comp.days_in_period, "", "", "", "", "", ""],
        # Row 6 (blank)
        ["", "", "", "", "", "", "", ""],
        # Row 7
        ["Total Final Amount", sum_formula, "", "", "", "", "", ""],
        # Row 8
        [
            "Slot Days Check",
            period_comp.validation.get("slot_days_check", ""),
            "",
            "",
            "",
            "",
            "",
            "",
        ],
    ]

    # Row 9: column headers (full 14-column row).
    header_row: list[object] = list(COMPENSATION_COLUMNS)

    # Data rows (10+).
    data_rows: list[list[object]] = []
    for r in rows_in:
        days_total = r.days_as_l1 + r.days_as_l2 + r.days_as_l3
        data_rows.append([
            r.name,
            _format_pct(r.participation_pct),
            _format_pct(r.communication_pct),
            r.metrics_modifier,
            _level_label(r.level_at_period_end),
            days_total,
            r.entitlement_pre_modifier,
            r.final_amount,
            r.rank_at_period_end if r.rank_at_period_end is not None else "",
            r.buffer_carry_in,
            r.buffer_added,
            r.payment_amount,
            r.buffer_post_payment,
            r.notes,
        ])

    # Combine: pad header block to 14 columns, then header_row, then data.
    n_cols = len(COMPENSATION_COLUMNS)

    def _pad(row: list[object]) -> list[object]:
        return row + [""] * (n_cols - len(row))

    all_values: list[list[object]] = [
        *(_pad(r) for r in header_block),
        header_row,
        *data_rows,
    ]

    worksheet = get_or_create_tab(
        workbook,
        _compensation_tab_title(period),
        rows=max(len(all_values) + 50, 100),
        cols=n_cols,
    )

    clear_tab(worksheet)

    end_cell = rowcol_to_a1(len(all_values), n_cols)
    worksheet.update(values=all_values, range_name=f"A1:{end_cell}")

    return worksheet


# ---------------------------------------------------------------------------
# Readers for finalize: Daily Data, Participation Raw Data, Communication Master
# ---------------------------------------------------------------------------


def _enumerate_months(start: date, end: date) -> list[MonthPeriod]:
    """Return one MonthPeriod per calendar month touched by [start, end].

    Inclusive on both ends. Handles year rollover.

    Returns:
        Months in chronological order.
    """
    months: list[MonthPeriod] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(MonthPeriod(year=y, month=m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months


def _parse_poll_history_tab(
    worksheet: gspread.Worksheet,
) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Validate the shared poll-history tab shape and return delegate cols + data rows.

    Tab layout: Poll Id | Start Date | End Date | Title | <Delegate 1> | ...

    Empty / header-only / no-delegate-column tabs and blank-poll-id rows
    are dropped.

    Returns:
        (delegate_columns, validated_rows). Each validated_rows entry
        is (poll_id_str, raw_row_cells); use _cell() to index per-
        delegate cells at `n_metadata + offset`.
    """
    rows = worksheet.get_all_values()
    if len(rows) < 2:
        return [], []

    header = rows[0]
    n_metadata = len(PARTICIPATION_METADATA_COLUMNS)
    if len(header) <= n_metadata:
        return [], []

    delegate_columns = header[n_metadata:]

    validated: list[tuple[str, list[str]]] = []
    for row in rows[1:]:
        if not row:
            continue
        poll_id = row[0].strip() if len(row) > 0 else ""
        if not poll_id:
            continue
        validated.append((poll_id, row))

    return delegate_columns, validated


def _cell(row: list[str], col_idx: int) -> str:
    """Return row[col_idx] or empty string if the row is too short.

    gspread returns short rows rather than padding with empties when
    operators leave trailing cells blank.
    """
    return row[col_idx] if col_idx < len(row) else ""


def read_daily_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
) -> dict[date, dict[str, int]]:
    """Read the workbook-wide Daily Data tab; return ranks for every day in `period`.

    Returns:
        {day: {delegate_name: rank}} for every day in `period` that
        appears in the tab.

    Raises:
        RuntimeError: if the Daily Data tab is absent, or no rows
            cover any `period` (Case A - operator never ran
            fetch for this period)
    """
    worksheet = _open_required_tab(
        workbook,
        DAILY_DATA_TAB_TITLE,
        f"Run `fetch` for {period} first to populate it.",
    )

    existing = _read_daily_data_existing(worksheet)

    result: dict[date, dict[str, int]] = {}
    period_start = period.start
    period_end = period.end
    for (date_str, delegate), (_sky, rank) in existing.items():
        try:
            day = date.fromisoformat(date_str)
        except ValueError:
            continue
        if not (period_start <= day <= period_end):
            continue
        result.setdefault(day, {})[delegate] = rank

    if not result:
        raise RuntimeError(
            f"'{DAILY_DATA_TAB_TITLE}' tab has no rows for {period} "
            f"({period_start} to {period_end}). Run `fetch` for {period} "
            f"before running `finalize`."
        )

    return result


def _read_poll_history_from_tab(
    worksheet: gspread.Worksheet,
) -> dict[str, list[tuple[str, date, str]]]:
    """Parse one Participation Raw Data tab into per-delegate poll history.

    Rows with an unparseable Start Date are skipped (no contribution to
    the window). Empty status cells are kept as empty strings; the
    eligibility computation handles them as not-votable.

    Returns:
        {delegate_name: [(poll_id, poll_start_date, participation_status), ...]}
        in row order.
    """
    delegate_columns, validated_rows = _parse_poll_history_tab(worksheet)
    if not delegate_columns:
        return {}

    n_metadata = len(PARTICIPATION_METADATA_COLUMNS)

    result: dict[str, list[tuple[str, date, str]]] = {name: [] for name in delegate_columns}

    for poll_id, row in validated_rows:
        start_str = _cell(row, 1)
        try:
            poll_start = date.fromisoformat(start_str)
        except ValueError:
            continue

        for offset, name in enumerate(delegate_columns):
            status = _cell(row, n_metadata + offset)
            result[name].append((poll_id, poll_start, status))

    return result


def read_participation_for_window(
    workbook: gspread.Spreadsheet,
    window_start: date,
    window_end: date,
) -> dict[str, list[tuple[str, date, str]]]:
    """Aggregate per-delegate poll history across all months in [window_start, window_end].

    Walks each month touching the window. For each, looks for a tab
    named "Participation Raw Data <Month Year>". Missing tabs are
    silently skipped (zero-poll months produce no tab). Filters polls
    by start date within the window bounds.

    Returns:
        {delegate_name: [(poll_id, poll_start_date, participation_status), ...]}
        aggregated across all available monthly tabs in the window.
    """
    months = _enumerate_months(window_start, window_end)

    aggregated: dict[str, list[tuple[str, date, str]]] = {}
    for month in months:
        tab_title = _participation_raw_data_tab_title(month)
        try:
            worksheet = workbook.worksheet(tab_title)
        except gspread.exceptions.WorksheetNotFound:
            continue

        per_delegate = _read_poll_history_from_tab(worksheet)
        for name, entries in per_delegate.items():
            bucket = aggregated.setdefault(name, [])
            for poll_id, poll_start, status in entries:
                if window_start <= poll_start <= window_end:
                    bucket.append((poll_id, poll_start, status))

    return aggregated


def read_communication_master(
    workbook: gspread.Spreadsheet,
) -> dict[str, dict[str, str]]:
    """Read the workbook-wide Communication Master tab.

    Keyed by delegate then poll_id for fast lookup at the join site
    (finalize iterates participation entries and needs the communication
    status per (delegate, poll) pair).

    Returns:
        {delegate_name: {poll_id_str: communication_status}}.
    """
    worksheet = _open_required_tab(
        workbook,
        COMMUNICATION_MASTER_TAB_TITLE,
        "Run `fetch` first to populate it.",
    )

    delegate_columns, validated_rows = _parse_poll_history_tab(worksheet)
    if not delegate_columns:
        return {}

    n_metadata = len(PARTICIPATION_METADATA_COLUMNS)

    result: dict[str, dict[str, str]] = {name: {} for name in delegate_columns}

    for poll_id, row in validated_rows:
        for offset, name in enumerate(delegate_columns):
            result[name][poll_id] = _cell(row, n_metadata + offset)

    return result
