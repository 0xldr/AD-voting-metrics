"""Google Sheets connection for writing the AD participation workbook.

Auth is via a Google Cloud service account; the JSON key file is referenced by GOOGLE_SERVICE_ACCOUNT_FILE and the
workbook by SHEETS_WORKBOOK_ID. The service account's client_email must be added as Editor on the workbook - a one-time
setup step in the Google Sheets sharing dialog.

I/O is mediated by gspread-dataframe so values move in and out of the sheet as pandas DataFrames. The writers for the
workbook-wide tabs (Daily Data, Communication Master) read the tab's current contents first so a fetch merges into
existing history rather than replacing it.
"""

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from gspread_dataframe import get_as_dataframe, set_with_dataframe

from .metrics import PARTICIPATED, PENDING_VERIFICATION, cross_reference_one
from .paths import BACKUP_DIR as _DEFAULT_BACKUP_DIR
from .period import MonthPeriod
from .settings import EnvSettings

logger = logging.getLogger(__name__)

# Scope required to read/write spreadsheets opened by ID. open_by_key uses
# the Sheets API only; Drive scopes are needed only for open-by-title/listing.
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


# ---------------------------------------------------------------------------
# Auth / workbook open / tab management
# ---------------------------------------------------------------------------


def get_workbook(
    service_account_file: str | Path | None = None,
    workbook_id: str | None = None,
) -> gspread.Spreadsheet:
    """Open the configured workbook using the service account credentials.

    If service_account_file or workbook_id is None, reads from the matching env var (GOOGLE_SERVICE_ACCOUNT_FILE /
    SHEETS_WORKBOOK_ID). Pass explicit values for testing.

    Returns:
        The opened gspread.Spreadsheet.

    Raises:
        RuntimeError: for any of:
          - Missing env var (when the corresponding arg is None)
          - Service account file path doesn't exist or isn't readable
          - JSON key file is malformed or wrong format
          - Workbook ID is wrong, or not shared with the service account
    """
    env = EnvSettings()
    if service_account_file is None:
        service_account_file = env.google_service_account_file
        if not service_account_file:
            raise RuntimeError(
                "GOOGLE_SERVICE_ACCOUNT_FILE environment variable is not set. "
                "Add it to your .env file (see .env.example), pointing at the "
                "JSON key file you download from Google Cloud Console.",
            )

    if workbook_id is None:
        workbook_id = env.sheets_workbook_id
        if not workbook_id:
            raise RuntimeError(
                "SHEETS_WORKBOOK_ID environment variable is not set. "
                "Add it to your .env file (see .env.example). The ID is the "
                "long string in the workbook URL between /d/ and /edit.",
            )

    sa_path = Path(service_account_file)
    if not sa_path.exists():
        msg = (
            f"Service account file not found at {sa_path}. Check GOOGLE_SERVICE_ACCOUNT_FILE in your .env file points "
            f"at the correct path."
        )
        raise RuntimeError(msg)
    if not sa_path.is_file():
        msg = f"Service account file path {sa_path} exists but is not a file."
        raise RuntimeError(msg)

    try:
        credentials = Credentials.from_service_account_file(
            str(sa_path),
            scopes=list(SCOPES),
        )
    except ValueError as e:
        msg = (
            f"Service account file at {sa_path} could not be parsed as a service-account JSON key. Original error: {e}"
        )
        raise RuntimeError(msg) from e

    client = gspread.authorize(credentials)

    try:
        return client.open_by_key(workbook_id)
    except gspread.exceptions.APIError as e:
        sa_email = credentials.service_account_email
        msg = (
            f"Could not open workbook {workbook_id!r}. Most likely the workbook isn't shared with the service account "
            f"email {sa_email}, or the workbook ID is wrong. Share the workbook with that email as Editor in Google "
            f"Sheets, then re-run. Original error: {e}"
        )
        raise RuntimeError(msg) from e


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

    `rows` and `cols` apply only on creation; existing tabs keep their current dimensions. They're keyword-only to force
    callers to size new tabs based on the data they're about to write.

    Title matching is exact and case-sensitive. Tab titles forbid `[`, `]`, `*`, `?`, `:`, `/`, `\` — not validated
    here; gspread will surface the API error.

    Returns:
        The worksheet.
    """
    try:
        return workbook.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        return workbook.add_worksheet(title=title, rows=rows, cols=cols)


# Local pre-clear backups of workbook-wide tabs. Module-level (not the paths
# constant directly) so tests can redirect it to a tmp dir.
BACKUP_DIR = _DEFAULT_BACKUP_DIR


def _backup_tab_before_clear(df: pd.DataFrame, tab_title: str) -> None:
    """Save a tab's parsed contents to a local CSV before a destructive clear+rewrite.

    Daily Data and Communication Master are workbook-wide: they hold history and operator edits that exist nowhere
    else, and their writers clear the tab before rewriting. A crash between clear and write would otherwise lose that
    data permanently; the CSV under output_data/backups/ makes it recoverable.

    Raises:
        RuntimeError: if the backup can't be written. Proceeding with the clear without a safety copy is worse than
            skipping this tab's update.
    """
    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z").replace(":", "-")
    path = BACKUP_DIR / f"{tab_title}_{timestamp}.csv"
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(path, index=False)
    except OSError as e:
        msg = f"Could not write pre-clear backup of '{tab_title}' to {path}: {e}. Refusing to clear the tab."
        raise RuntimeError(msg) from e
    logger.info("Pre-clear backup of '%s' written to %s", tab_title, path)


# ---------------------------------------------------------------------------
# Shared read/write helpers
# ---------------------------------------------------------------------------


def _read_sheet_as_strings(worksheet: gspread.Worksheet) -> pd.DataFrame:
    """Read a worksheet via gspread-dataframe with all cells as strings.

    Empty cells come through as "" rather than NaN; trailing all-blank rows and columns are dropped by
    gspread-dataframe.

    Returns a DataFrame with string cells, or empty DataFrame if the tab is empty.

    """
    df = cast("pd.DataFrame", get_as_dataframe(worksheet, dtype=str, na_filter=False, header=0))
    df.fillna("", inplace=True)  # noqa: PD002 — keep DataFrame type concrete for downstream callers
    return df


def _coerce_date(value: date | datetime | None) -> str:
    """Convert a date/datetime/pd.Timestamp/None to a 'YYYY-MM-DD' string.

    None becomes empty string. Datetimes (and pd.Timestamp, which subclasses datetime) collapse to their date portion.


    """
    d = _to_date_value(value)
    return "" if d is None else d.isoformat()


def _metadata_by_id(poll_info: list[dict], spell_info: list[dict]) -> dict[str, dict]:
    """Index poll and spell records by stringified ID/address for O(1) lookup.

    Returns a mapping of str(pollId) / str(address) to its record. Polls win an (unexpected) key collision, matching
    the poll-first search order this replaces.
    """
    out: dict[str, dict] = {str(spell["address"]): spell for spell in spell_info}
    out.update({str(poll["pollId"]): poll for poll in poll_info})
    return out


# ---------------------------------------------------------------------------
# Daily Data tab (workbook-wide, long format)
# ---------------------------------------------------------------------------


DAILY_DATA_COLUMNS: tuple[str, ...] = ("Date", "Delegate", "Total Delegation", "Rank")
DAILY_DATA_TAB_TITLE = "Daily Data"


def _existing_daily_data(worksheet: gspread.Worksheet) -> pd.DataFrame:
    """Read the Daily Data tab into a typed DataFrame.

    Returns an empty DataFrame (with the expected columns) when the tab is empty, has an unrecognised header, or has no
    parseable rows. The caller can then treat it as a fresh write.

    Returns a DataFrame with columns Date (date), Delegate (str), Total Delegation (float), Rank (int).

    """
    df = _read_sheet_as_strings(worksheet)
    if df.empty or list(df.columns[: len(DAILY_DATA_COLUMNS)]) != list(DAILY_DATA_COLUMNS):
        return pd.DataFrame(columns=list(DAILY_DATA_COLUMNS))

    df = df[list(DAILY_DATA_COLUMNS)]
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.date
    df["Total Delegation"] = pd.to_numeric(df["Total Delegation"], errors="coerce")
    df["Rank"] = pd.to_numeric(df["Rank"], errors="coerce")
    df = df.dropna(subset=["Date", "Delegate", "Total Delegation", "Rank"])
    df = df[df["Delegate"].astype(str).str.strip() != ""]
    df["Rank"] = df["Rank"].astype(int)
    return df.reset_index(drop=True)


def _row_counts_by_date(frame: pd.DataFrame) -> dict[date, int]:
    """Count rows per Date in a Daily Data frame.

    Callers pass frames whose Date column already holds date objects (nulls, if any, are dropped by value_counts).

    Returns a mapping of date to the number of rows on that date.

    """
    return {cast("date", d): int(n) for d, n in frame["Date"].value_counts().items()}


def write_daily_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df_ranking: pd.DataFrame,
) -> gspread.Worksheet:
    """Write the workbook-wide Daily Data tab, merging in the current fetch.

    Long format, one row per (date, delegate). Columns are exactly DAILY_DATA_COLUMNS; extras in df_ranking are ignored.

    Merge behavior:
      - Existing rows for dates not in the current fetch are preserved.
      - Existing rows for dates in the current fetch are overwritten (re-runs are idempotent).
      - New dates are added.

    For dates appearing in both existing and new data, the count of delegate rows must match - a mismatch indicates
    roster drift (delegate added/removed) and the function raises rather than silently shifting which delegates are
    represented.

    Date values are coerced to 'YYYY-MM-DD' ISO strings. Output is sorted by (Date asc, Rank asc).

    Tab name is "Daily Data" (no period suffix); `period` is retained for error messages.

    Returns:
        The worksheet that was written.

    Raises:
        ValueError: if df_ranking is missing required columns, or on roster drift.
    """
    missing = [c for c in DAILY_DATA_COLUMNS if c not in df_ranking.columns]
    if missing:
        msg = f"df_ranking is missing required columns: {missing}. Has: {list(df_ranking.columns)}"
        raise ValueError(msg)

    new_df = df_ranking[list(DAILY_DATA_COLUMNS)]
    new_df["Date"] = new_df["Date"].apply(_to_date_value)
    new_df["Total Delegation"] = new_df["Total Delegation"].astype(float)
    new_df["Rank"] = new_df["Rank"].astype(int)

    worksheet = get_or_create_tab(
        workbook,
        DAILY_DATA_TAB_TITLE,
        rows=max(len(new_df) + 100, 200),
        cols=max(len(DAILY_DATA_COLUMNS), 4),
    )

    existing = _existing_daily_data(worksheet)

    new_counts = _row_counts_by_date(new_df)

    if not existing.empty:
        overlap = existing[existing["Date"].isin(new_counts.keys())]
        existing_counts = _row_counts_by_date(overlap)
        for date_val, new_count in new_counts.items():
            existing_count = existing_counts.get(date_val)
            if existing_count is not None and existing_count != new_count:
                msg = (
                    f"Roster drift detected for date {date_val.isoformat()}: existing Daily Data has "
                    f"{existing_count} delegate rows, current fetch for {period} has {new_count}. The active-"
                    f"delegate count for that date changed between fetches. Reconcile manually (roster YAML vs "
                    f"Communication Master / Daily Data) before re-running."
                )
                raise ValueError(msg)
        keep = existing[~existing["Date"].isin(new_counts.keys())]
        merged = pd.concat([keep, new_df], ignore_index=True)
    else:
        merged = new_df

    merged = merged.sort_values(by=["Date", "Rank"]).reset_index(drop=True)
    merged["Date"] = merged["Date"].apply(_coerce_date)

    if not existing.empty:
        _backup_tab_before_clear(existing, DAILY_DATA_TAB_TITLE)
    worksheet.clear()
    set_with_dataframe(
        worksheet, merged, include_index=False, include_column_header=True, resize=False, allow_formulas=False
    )
    return worksheet


def _to_date_value(value: date | datetime | str | None) -> date | None:
    """Best-effort conversion of an arbitrary input to a date object.

    Strings are parsed as ISO dates; datetimes (incl. pd.Timestamp) collapse to their date portion; None stays None.

    Returns date or None if value can't be parsed.

    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Participation Raw Data tab (per-period, wide format)
# ---------------------------------------------------------------------------


PARTICIPATION_METADATA_COLUMNS: tuple[str, ...] = (
    "Poll Id",
    "Start Date",
    "End Date",
    "Title",
)


def _participation_raw_data_tab_title(period: MonthPeriod) -> str:
    """Return the per-month tab title, e.g. "Participation Raw Data April 2026"."""
    return f"Participation Raw Data {period}"


def build_participation_dataframe(
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> pd.DataFrame:
    """Build the wide-format Participation Raw Data DataFrame.

    Input df shape: one row per delegate, with fixed columns
    'Delegate Name', 'Delegate Contract', 'Start Date', then one column
    per poll_id (str) and one per spell address.

    Output shape: one row per poll/spell with columns
    [Poll Id, Start Date, End Date, Title, <Delegate 1>, ...]. Zero-poll
    months return a header-only DataFrame.

    Poll/spell columns with no matching poll_info or spell_info entry
    get blank metadata cells; the status column is still written so a
    transient API inconsistency can't drop participation data. Spell
    rows have blank End Date - spells don't carry endDate in spell_info.

    Returns:
        Wide-format participation DataFrame.
    """
    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [c for c in df.columns if c not in fixed_cols]
    columns = [*PARTICIPATION_METADATA_COLUMNS, *delegate_names]

    if not poll_columns:
        return pd.DataFrame(columns=columns)

    df_by_delegate = df.set_index("Delegate Name")
    metadata_by_id = _metadata_by_id(poll_info, spell_info)
    rows: list[dict] = []
    for poll_id in poll_columns:
        metadata = metadata_by_id.get(str(poll_id), {})
        row: dict = {
            "Poll Id": str(poll_id),
            "Start Date": _coerce_date(metadata.get("startDate")),
            "End Date": _coerce_date(metadata.get("endDate")),
            "Title": str(metadata.get("title", "")),
        }
        for name in delegate_names:
            row[name] = str(df_by_delegate.loc[name, poll_id])
        rows.append(row)

    return pd.DataFrame(rows, columns=columns)


def write_participation_raw_data(
    workbook: gspread.Spreadsheet,
    period: MonthPeriod,
    df: pd.DataFrame,
    poll_info: list[dict],
    spell_info: list[dict],
) -> gspread.Worksheet:
    """Write the per-period Participation Raw Data tab. Re-runs overwrite.

    Layout: one row per poll/spell with metadata + per-delegate status.
    Columns: Poll Id | Start Date | End Date | Title | <Delegate 1> | ...

    Tab is named "Participation Raw Data {period}".

    Returns:
        The worksheet that was written.
    """
    out_df = build_participation_dataframe(df, poll_info, spell_info)

    tab_title = _participation_raw_data_tab_title(period)
    worksheet = get_or_create_tab(
        workbook,
        tab_title,
        rows=max(len(out_df) + 10, 100),
        cols=max(len(out_df.columns), len(PARTICIPATION_METADATA_COLUMNS) + 1),
    )

    worksheet.clear()
    set_with_dataframe(
        worksheet, out_df, include_index=False, include_column_header=True, resize=False, allow_formulas=False
    )
    return worksheet


# ---------------------------------------------------------------------------
# Communication Master tab (workbook-wide, wide format)
# ---------------------------------------------------------------------------


COMMUNICATION_MASTER_TAB_TITLE = "Communication Master"


def _existing_comm_master(worksheet: gspread.Worksheet) -> pd.DataFrame:
    """Read the Communication Master tab as a string DataFrame indexed on Poll Id.

    Blank-poll-id rows are dropped. Empty tabs return an empty
    DataFrame with no columns; the writer detects this and seeds the
    header from the current roster.

    Returns a DataFrame indexed by Poll Id (string), or empty DataFrame.

    """
    df = _read_sheet_as_strings(worksheet)
    if df.empty or "Poll Id" not in df.columns:
        return pd.DataFrame()
    df["Poll Id"] = df["Poll Id"].astype(str)
    df = df[df["Poll Id"].str.strip() != ""]
    if df.empty:
        return pd.DataFrame(columns=df.columns)
    return df.set_index("Poll Id")


def _build_comm_defaults(
    df: pd.DataFrame,
    columns: list[str],
    poll_columns: list[str],
    poll_info: list[dict],
    spell_info: list[dict],
) -> pd.DataFrame:
    """Build the "fresh write" DataFrame for the current fetch's polls/spells.

    Per-cell defaults follow the cross-reference rule for in-roster
    delegates; out-of-roster columns stay blank.

    Returns a DataFrame indexed by poll_id (string) with the given columns.

    """
    df_by_delegate = df.set_index("Delegate Name")
    in_roster = set(df_by_delegate.index)
    metadata_by_id = _metadata_by_id(poll_info, spell_info)

    rows: dict[str, dict] = {}
    for poll_id in poll_columns:
        metadata = metadata_by_id.get(poll_id, {})
        row: dict = {
            "Start Date": _coerce_date(metadata.get("startDate")),
            "End Date": _coerce_date(metadata.get("endDate")),
            "Title": str(metadata.get("title", "")),
        }
        for col in columns:
            if col in row:
                continue
            if col not in in_roster:
                row[col] = ""
                continue
            # Apply the cross-reference rule from metrics, with the sheets-specific
            # defaults: PARTICIPATED becomes the "Pending verification" sentinel
            # (operator must confirm), unknown becomes blank.
            p_status = str(df_by_delegate.loc[col, poll_id]) if poll_id in df_by_delegate.columns else ""
            override = cross_reference_one(p_status)
            if override is not None:
                row[col] = override
            elif p_status in PARTICIPATED:
                row[col] = PENDING_VERIFICATION
            else:
                row[col] = ""
        rows[poll_id] = row

    out = pd.DataFrame.from_dict(rows, orient="index", columns=columns)
    out.index.name = "Poll Id"
    return out


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
        raise ValueError("df must have a 'Delegate Name' column")

    delegate_names = df["Delegate Name"].tolist()
    fixed_cols = {"Delegate Name", "Delegate Contract", "Start Date"}
    poll_columns = [str(c) for c in df.columns if c not in fixed_cols]

    worksheet = get_or_create_tab(
        workbook,
        COMMUNICATION_MASTER_TAB_TITLE,
        rows=max(len(poll_columns) + 100, 200),
        cols=max(len(PARTICIPATION_METADATA_COLUMNS) + len(delegate_names), 10),
    )

    existing = _existing_comm_master(worksheet)

    if existing.empty or len(existing.columns) == 0:
        columns = [
            *(c for c in PARTICIPATION_METADATA_COLUMNS if c != "Poll Id"),
            *delegate_names,
        ]
        existing = pd.DataFrame(columns=columns)
        existing.index.name = "Poll Id"
    else:
        missing = [n for n in delegate_names if n not in existing.columns]
        if missing:
            msg = (
                f"Communication Master is missing column(s) for delegate(s): "
                f"{missing}. Add a column header with the exact delegate name "
                f"for each missing delegate to the Communication Master tab, "
                f"then re-run. (Columns can't be auto-added - operators control "
                f"column placement and naming.)"
            )
            raise ValueError(msg)

    columns = list(existing.columns)
    defaults = _build_comm_defaults(df, columns, poll_columns, poll_info, spell_info)

    # Merge rule: keep any operator-entered cell (non-blank); for blank cells
    # (and rows that are in this fetch but not yet in the tab), fall back to
    # defaults. `combine_first` does the cell-level fill + index/column union
    # in one shot once empty/whitespace cells are treated as null.
    merged = existing.map(str.strip).replace("", pd.NA).combine_first(defaults).fillna("")

    merged = merged.sort_values(
        "Start Date",
        key=lambda c: pd.to_datetime(c, errors="coerce"),
        ascending=False,
        na_position="last",
    )

    out = merged.reset_index()

    if not existing.empty:
        _backup_tab_before_clear(existing.reset_index(), COMMUNICATION_MASTER_TAB_TITLE)
    worksheet.clear()
    set_with_dataframe(
        worksheet, out, include_index=False, include_column_header=True, resize=False, allow_formulas=False
    )
    return worksheet
