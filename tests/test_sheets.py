"""Tests for the sheets module — auth + workbook connection.

Unit tests mock gspread and google-auth so they don't hit live Google. The
single integration test (marked @pytest.mark.integration) is skipped by
default and runs only with `pytest -m integration`; it requires real env
vars and verifies end-to-end connectivity.
"""

import json
import uuid
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import gspread
import pandas as pd
import pytest

import ad_voting_metrics.sheets as sheets
from ad_voting_metrics.period import MonthPeriod

# ---------------------------------------------------------------------------
# SCOPES — pinned so an accidental edit fails the test
# ---------------------------------------------------------------------------


def test_scopes_are_sheets_and_drive():
    """Sheets scope for cell I/O; Drive scope required for open_by_key."""
    assert sheets.SCOPES == (
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    )


# ---------------------------------------------------------------------------
# get_workbook — env var validation
# ---------------------------------------------------------------------------


def test_get_workbook_missing_service_account_env_raises(monkeypatch):
    """Without the env var and without an explicit arg, fail clearly."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.setenv("SHEETS_WORKBOOK_ID", "anything")
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        sheets.get_workbook()


def test_get_workbook_missing_workbook_id_env_raises(monkeypatch, tmp_path):
    """Fail clearly when SHEETS_WORKBOOK_ID is missing."""
    fake_sa = tmp_path / "fake.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(fake_sa))
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="SHEETS_WORKBOOK_ID"):
        sheets.get_workbook()


def test_get_workbook_both_env_vars_missing_raises_service_account_first(monkeypatch):
    """When both env vars are missing, surface the service-account error first (check order)."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        sheets.get_workbook()


def test_get_workbook_error_message_points_at_env_example(monkeypatch):
    """The error message tells the operator where to look for the fix."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    with pytest.raises(RuntimeError, match=r"\.env\.example"):
        sheets.get_workbook(workbook_id="anything")


# ---------------------------------------------------------------------------
# get_workbook — file validation
# ---------------------------------------------------------------------------


def test_get_workbook_service_account_file_missing_raises(tmp_path):
    """File path is valid string but the file doesn't exist."""
    missing = tmp_path / "nonexistent.json"
    with pytest.raises(RuntimeError, match="not found"):
        sheets.get_workbook(
            service_account_file=missing,
            workbook_id="anything",
        )


def test_get_workbook_service_account_path_is_directory_raises(tmp_path):
    """Raise a distinct error when the service-account path is a directory."""
    with pytest.raises(RuntimeError, match="not a file"):
        sheets.get_workbook(
            service_account_file=tmp_path,
            workbook_id="anything",
        )


# ---------------------------------------------------------------------------
# get_workbook — credentials parsing
# ---------------------------------------------------------------------------


def test_get_workbook_malformed_json_raises(tmp_path):
    """Wrap google-auth's ValueError on bad JSON as a RuntimeError with context."""
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json")
    with pytest.raises(RuntimeError, match="could not be parsed"):
        sheets.get_workbook(
            service_account_file=bad_file,
            workbook_id="anything",
        )


def test_get_workbook_wrong_key_type_raises(tmp_path):
    """Valid JSON but not a service-account key (e.g., user creds) -> same error path as malformed."""
    not_sa = tmp_path / "not-sa.json"
    not_sa.write_text(json.dumps({"type": "oauth", "client_id": "abc"}))
    with pytest.raises(RuntimeError, match="could not be parsed"):
        sheets.get_workbook(
            service_account_file=not_sa,
            workbook_id="anything",
        )


# ---------------------------------------------------------------------------
# get_workbook — happy path (mocked) and api error handling
# ---------------------------------------------------------------------------


def _make_fake_sa_file(tmp_path: Path) -> Path:
    """Write a syntactically-valid service-account JSON file - credential loading is patched."""
    sa = tmp_path / "sa.json"
    sa.write_text(
        json.dumps({
            "type": "service_account",
            "project_id": "fake-project",
            "client_email": "fake@fake-project.iam.gserviceaccount.com",
        })
    )
    return sa


def test_get_workbook_happy_path_returns_spreadsheet(tmp_path):
    """When everything works, return the gspread Spreadsheet."""
    sa_file = _make_fake_sa_file(tmp_path)
    fake_spreadsheet = MagicMock(spec=gspread.Spreadsheet)
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = fake_spreadsheet
    fake_creds = MagicMock()

    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=fake_creds),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
    ):
        result = sheets.get_workbook(
            service_account_file=sa_file,
            workbook_id="WORKBOOK_ID_123",
        )

    assert result is fake_spreadsheet
    fake_client.open_by_key.assert_called_once_with("WORKBOOK_ID_123")


def test_get_workbook_passes_correct_scopes_to_credentials(tmp_path):
    """Pass sheets.SCOPES to Credentials.from_service_account_file."""
    sa_file = _make_fake_sa_file(tmp_path)

    with (
        patch.object(sheets.Credentials, "from_service_account_file") as mock_from_file,
        patch.object(sheets.gspread, "authorize"),
    ):
        mock_from_file.return_value = MagicMock()
        sheets.get_workbook(
            service_account_file=sa_file,
            workbook_id="anything",
        )

    call_kwargs = mock_from_file.call_args.kwargs
    assert call_kwargs["scopes"] == list(sheets.SCOPES)


def test_get_workbook_api_error_on_open_wrapped_with_context(tmp_path):
    """Wrap gspread's APIError (wrong ID, not shared) with a message naming the service account."""
    sa_file = _make_fake_sa_file(tmp_path)
    fake_creds = MagicMock()
    fake_creds.service_account_email = "fake@fake-project.iam.gserviceaccount.com"
    fake_client = MagicMock()
    fake_client.open_by_key.side_effect = gspread.exceptions.APIError(
        # gspread.exceptions.APIError takes a response-like object in v6;
        # MagicMock satisfies the constructor without real HTTP machinery.
        MagicMock(status_code=403, text="not shared")
    )

    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=fake_creds),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
        pytest.raises(RuntimeError, match="shared with the service account"),
    ):
        sheets.get_workbook(
            service_account_file=sa_file,
            workbook_id="some-id",
        )


def test_get_workbook_api_error_message_includes_sa_email(tmp_path):
    """Wrapped error names the service account email so the operator knows what to share with."""
    sa_file = _make_fake_sa_file(tmp_path)
    fake_creds = MagicMock()
    fake_creds.service_account_email = "scripted@my-project.iam.gserviceaccount.com"
    fake_client = MagicMock()
    fake_client.open_by_key.side_effect = gspread.exceptions.APIError(
        MagicMock(status_code=403, text="not shared")
    )

    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=fake_creds),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
        pytest.raises(RuntimeError, match=r"scripted@my-project\.iam\.gserviceaccount\.com"),
    ):
        sheets.get_workbook(
            service_account_file=sa_file,
            workbook_id="some-id",
        )


def test_get_workbook_explicit_args_override_env(monkeypatch, tmp_path):
    """When both args are provided, env vars are ignored entirely."""
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/should/not/be/used")
    monkeypatch.setenv("SHEETS_WORKBOOK_ID", "wrong-id")

    sa_file = _make_fake_sa_file(tmp_path)
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = MagicMock(spec=gspread.Spreadsheet)

    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=MagicMock()),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
    ):
        sheets.get_workbook(
            service_account_file=sa_file,
            workbook_id="correct-id",
        )

    fake_client.open_by_key.assert_called_once_with("correct-id")


# ---------------------------------------------------------------------------
# Integration test — opt-in, hits real Google. Skipped by default.
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_workbook_real_credentials_opens_spreadsheet():
    """End-to-end smoke test against the real configured workbook.

    Run with `pytest -m integration`. Requires GOOGLE_SERVICE_ACCOUNT_FILE
    and SHEETS_WORKBOOK_ID in the environment. Read-only.
    """
    workbook = sheets.get_workbook()
    # Sanity check: a real Spreadsheet has a title attribute that hits
    # the API. If auth or sharing is broken, this raises here.
    assert isinstance(workbook.title, str)
    assert workbook.title  # non-empty


# ---------------------------------------------------------------------------
# Tab management — list_tab_names, get_or_create_tab, clear_tab
# ---------------------------------------------------------------------------


def test_list_tab_names_returns_titles_in_order():
    """Wraps workbook.worksheets() and returns just the titles."""
    fake_ws1 = MagicMock(title="Daily Data")
    fake_ws2 = MagicMock(title="Participation Raw Data")
    fake_ws3 = MagicMock(title="Compensation")
    workbook = MagicMock()
    workbook.worksheets.return_value = [fake_ws1, fake_ws2, fake_ws3]

    result = sheets.list_tab_names(workbook)

    assert result == ["Daily Data", "Participation Raw Data", "Compensation"]


def test_list_tab_names_empty_workbook_returns_empty_list():
    """Return [] for a workbook with no tabs (impossible in practice; handle gracefully)."""
    workbook = MagicMock()
    workbook.worksheets.return_value = []

    assert sheets.list_tab_names(workbook) == []


def test_get_or_create_tab_returns_existing_tab():
    """If the worksheet exists, return it without trying to create."""
    existing_ws = MagicMock(spec=gspread.Worksheet)
    workbook = MagicMock()
    workbook.worksheet.return_value = existing_ws

    result = sheets.get_or_create_tab(workbook, "Daily Data", rows=100, cols=10)

    assert result is existing_ws
    workbook.worksheet.assert_called_once_with("Daily Data")
    workbook.add_worksheet.assert_not_called()


def test_get_or_create_tab_creates_when_missing():
    """If the worksheet doesn't exist, create it with the given dimensions."""
    created_ws = MagicMock(spec=gspread.Worksheet)
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = created_ws

    result = sheets.get_or_create_tab(workbook, "New Tab", rows=500, cols=20)

    assert result is created_ws
    workbook.add_worksheet.assert_called_once_with(title="New Tab", rows=500, cols=20)


def test_get_or_create_tab_does_not_resize_existing_tab():
    """Return existing tab as-is regardless of the rows/cols passed."""
    existing_ws = MagicMock(spec=gspread.Worksheet)
    workbook = MagicMock()
    workbook.worksheet.return_value = existing_ws

    sheets.get_or_create_tab(workbook, "Existing", rows=999, cols=99)

    # add_worksheet not called → no resize attempt
    workbook.add_worksheet.assert_not_called()
    # existing_ws.resize() should not be called either
    existing_ws.resize.assert_not_called()


def test_get_or_create_tab_requires_keyword_only_rows_cols():
    """Raise TypeError when rows/cols are passed positionally (forces explicit sizing)."""
    workbook = MagicMock()
    workbook.worksheet.return_value = MagicMock(spec=gspread.Worksheet)

    # getattr indirection so static type-checkers don't flag the deliberate
    # positional misuse below; we're testing runtime rejection.
    fn = getattr(sheets, "get_or_create_tab")  # noqa: B009
    with pytest.raises(TypeError):
        # Attempting positional args should fail
        fn(workbook, "Tab", 100, 10)


def test_clear_tab_calls_worksheet_clear():
    """Delegate to gspread's worksheet.clear() (wipes values, preserves formatting)."""
    worksheet = MagicMock(spec=gspread.Worksheet)

    sheets.clear_tab(worksheet)

    worksheet.clear.assert_called_once()


# ---------------------------------------------------------------------------
# Tab management — integration tests against the real workbook
# ---------------------------------------------------------------------------


@pytest.fixture
def _temp_tab(request):
    """Yield a unique temp-tab title; clean up the tab after the test even on failure."""
    tab_name = f"_test_temp_{uuid.uuid4().hex[:8]}"

    def cleanup():
        try:
            workbook = sheets.get_workbook()
            try:
                ws = workbook.worksheet(tab_name)
                workbook.del_worksheet(ws)
            except gspread.exceptions.WorksheetNotFound:
                pass
        except Exception:
            # Don't let cleanup failure mask the test result. Operator can
            # manually delete any leftover _test_temp_* tabs
            pass

    request.addfinalizer(cleanup)
    return tab_name


@pytest.mark.integration
def test_list_tab_names_returns_real_tabs():
    """The real workbook has at least one tab; list returns it."""
    workbook = sheets.get_workbook()
    names = sheets.list_tab_names(workbook)
    assert len(names) >= 1
    assert all(isinstance(n, str) for n in names)


@pytest.mark.integration
def test_get_or_create_tab_creates_and_finds_real_tab(_temp_tab):
    """Create a real tab, verify it appears, fetch it again, get the same one."""
    workbook = sheets.get_workbook()

    # First call creates
    created = sheets.get_or_create_tab(workbook, _temp_tab, rows=50, cols=5)
    assert created.title == _temp_tab
    assert _temp_tab in sheets.list_tab_names(workbook)

    # Second call returns the same tab (doesn't create a duplicate)
    fetched = sheets.get_or_create_tab(workbook, _temp_tab, rows=999, cols=99)
    assert fetched.id == created.id


@pytest.mark.integration
def test_clear_tab_wipes_real_cells(_temp_tab):
    """Write some data, clear, verify the cells are empty."""
    workbook = sheets.get_workbook()
    ws = sheets.get_or_create_tab(workbook, _temp_tab, rows=10, cols=3)

    # Write some data
    ws.update(values=[["hello", "world"], ["foo", "bar"]], range_name="A1:B2")

    # Verify it's there
    assert ws.acell("A1").value == "hello"

    # Clear
    sheets.clear_tab(ws)

    # Verify it's gone
    assert ws.acell("A1").value is None


# ---------------------------------------------------------------------------
# write_daily_data — Daily Data tab writer
# ---------------------------------------------------------------------------


def _make_ranking_df():
    """Build a small df_ranking-shaped DataFrame: Date, Delegate, Total Delegation, Rank"""
    return pd.DataFrame({
        "Date": [date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2)],
        "Delegate": ["BLUE", "Cloaky", "BLUE"],
        "Total Delegation": [1234567.89, 987654.32, 1234999.99],
        "Rank": [1, 2, 1],
    })


def test_daily_data_columns_pinned():
    """The column tuple is pinned so an accidental edit fails the test."""
    assert sheets.DAILY_DATA_COLUMNS == ("Date", "Delegate", "Total Delegation", "Rank")


def test_daily_data_tab_title_constant():
    """Workbook-wide tab name (no period suffix)."""
    assert sheets.DAILY_DATA_TAB_TITLE == "Daily Data"


def _empty_existing_ws(workbook):
    """MagicMock worksheet returning no existing rows; for first-fetch tests where the tab is empty"""
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = []
    workbook.worksheet.return_value = fake_ws
    return fake_ws


def test_write_daily_data_missing_columns_raises():
    """The dataframe must have at least the four expected columns."""
    df_bad = pd.DataFrame({"Date": [date(2026, 4, 1)], "Delegate": ["BLUE"]})
    workbook = MagicMock()
    period = MonthPeriod(year=2026, month=4)

    with pytest.raises(ValueError, match="missing required columns"):
        sheets.write_daily_data(workbook, period, df_bad)


def test_write_daily_data_uses_workbook_wide_tab_name():
    """The tab name is 'Daily Data' regardless of period - workbook-wide."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = []
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    call = workbook.add_worksheet.call_args
    assert call.kwargs["title"] == "Daily Data"


def test_write_daily_data_first_fetch_writes_header_and_rows():
    """First-ever fetch: empty existing tab, writes header + data."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0] == ["Date", "Delegate", "Total Delegation", "Rank"]
    # 3 data rows from fixture, sorted (date asc, rank asc) — fixture already
    # in that order: (Apr 1, BLUE, 1), (Apr 1, Cloaky, 2), (Apr 2, BLUE, 1)
    assert len(values) == 4
    assert values[1] == ["2026-04-01", "BLUE", 1234567.89, 1]
    assert values[2] == ["2026-04-01", "Cloaky", 987654.32, 2]
    assert values[3] == ["2026-04-02", "BLUE", 1234999.99, 1]


def test_write_daily_data_clears_before_writing():
    """Always clear before writing the merged state."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    fake_ws.clear.assert_called_once()
    fake_ws.update.assert_called_once()


def test_write_daily_data_preserves_existing_rows_for_other_dates():
    """Preserve existing rows for dates not in the current fetch (tab is workbook-wide)."""
    # Current fetch covers April 2026, rank-1 BLUE only
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1)],
        "Delegate": ["BLUE"],
        "Total Delegation": [100.0],
        "Rank": [1],
    })
    # Existing tab has a March 2026 row (different month, should be preserved)
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-03-15", "BLUE", "50.0", "2"],
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Header + March row (preserved) + April row (new) = 3 rows
    assert len(values) == 3
    # Sort order is (date asc, rank asc), so March first
    assert values[1] == ["2026-03-15", "BLUE", 50.0, 2]
    assert values[2] == ["2026-04-01", "BLUE", 100.0, 1]


def test_write_daily_data_overwrites_existing_rows_for_current_dates():
    """Overwrite existing rows for dates in the current fetch (re-runs are idempotent)."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1)],
        "Delegate": ["BLUE"],
        "Total Delegation": [999.99],  # different from existing 100.0
        "Rank": [1],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-04-01", "BLUE", "100.0", "5"],  # stale, should be replaced
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Just header + the overwritten April row
    assert len(values) == 2
    # Values from the new df, not the existing tab
    assert values[1] == ["2026-04-01", "BLUE", 999.99, 1]


def test_write_daily_data_raises_on_roster_drift():
    """Raise when a shared date has different delegate counts (roster changed mid-period)."""
    # Current fetch has 2 delegates for April 1
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Delegate": ["BLUE", "Cloaky"],
        "Total Delegation": [100.0, 200.0],
        "Rank": [1, 2],
    })
    # Existing tab has 3 delegates for April 1 (drift!)
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-04-01", "BLUE", "100.0", "1"],
        ["2026-04-01", "Cloaky", "200.0", "2"],
        ["2026-04-01", "OldDelegate", "50.0", "3"],  # extra
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    with pytest.raises(ValueError, match="Roster drift"):
        sheets.write_daily_data(workbook, period, df)


def test_write_daily_data_no_drift_when_dates_dont_overlap():
    """Skip the drift check when existing and new data share no dates."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Delegate": ["BLUE", "Cloaky"],
        "Total Delegation": [100.0, 200.0],
        "Rank": [1, 2],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    # March data has 1 delegate, April has 2 — different counts but different
    # dates, so no drift check fires.
    fake_ws.get_all_values.return_value = [
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-03-15", "BLUE", "50.0", "1"],
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    # Should not raise
    sheets.write_daily_data(workbook, period, df)


def test_write_daily_data_handles_unparseable_existing_rows():
    """Drop malformed rows from the existing tab rather than crashing the whole write."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1)],
        "Delegate": ["BLUE"],
        "Total Delegation": [100.0],
        "Rank": [1],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-03-15", "BLUE", "not-a-number", "1"],  # bad numeric → dropped
        ["2026-03-16", "Cloaky", "50.0", "2"],  # good
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Header + March 16 good row + April 1 new row = 3 rows (Mar 15 dropped)
    assert len(values) == 3


def test_write_daily_data_handles_unrecognised_existing_header():
    """Treat an existing tab with wrong-shape header shape as empty; clear-and-rewrite replaces it."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1)],
        "Delegate": ["BLUE"],
        "Total Delegation": [100.0],
        "Rank": [1],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["WrongColumn", "Stuff", "Here", "Doesn'tMatter"],
        ["junk", "junk", "junk", "junk"],
    ]
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Just header + the new April row; existing junk discarded
    assert len(values) == 2
    assert values[0] == ["Date", "Delegate", "Total Delegation", "Rank"]


def test_write_daily_data_sort_order_date_then_rank():
    """Merged output is sorted by (Date ascending, Rank ascending)."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 2), date(2026, 4, 1), date(2026, 4, 1)],
        "Delegate": ["BLUE", "Cloaky", "BLUE"],
        "Total Delegation": [10.0, 20.0, 30.0],
        "Rank": [1, 2, 1],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Expect: April 1 rank 1 (BLUE), April 1 rank 2 (Cloaky), April 2 rank 1 (BLUE)
    assert values[1] == ["2026-04-01", "BLUE", 30.0, 1]
    assert values[2] == ["2026-04-01", "Cloaky", 20.0, 2]
    assert values[3] == ["2026-04-02", "BLUE", 10.0, 1]


def test_write_daily_data_range_matches_data_shape():
    """The A1 range passed to update() matches the values shape."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    update_kwargs = fake_ws.update.call_args.kwargs
    # 4 columns, 4 rows (header + 3 data) — fixture has 3 distinct rows
    assert update_kwargs["range_name"] == "A1:D4"


def test_write_daily_data_handles_pandas_timestamps():
    """Dates can arrive as pandas Timestamps; they get ISO-formatted."""
    df = pd.DataFrame({
        "Date": pd.to_datetime(["2026-04-01", "2026-04-02"]),
        "Delegate": ["BLUE", "Cloaky"],
        "Total Delegation": [100.0, 200.0],
        "Rank": [1, 2],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[1][0] == "2026-04-01"
    assert values[2][0] == "2026-04-02"


def test_write_daily_data_extra_columns_ignored():
    """Extra columns in df_ranking beyond the canonical four are ignored."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1)],
        "Delegate": ["BLUE"],
        "Total Delegation": [100.0],
        "Rank": [1],
        "ExtraColumn": ["ignored"],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0] == ["Date", "Delegate", "Total Delegation", "Rank"]
    assert len(values[1]) == 4
    assert "ignored" not in values[1]


def test_write_daily_data_empty_dataframe_with_empty_existing():
    """No new rows AND no existing rows: writes header only."""
    df = pd.DataFrame(columns=["Date", "Delegate", "Total Delegation", "Rank"])
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values == [["Date", "Delegate", "Total Delegation", "Rank"]]
    assert fake_ws.update.call_args.kwargs["range_name"] == "A1:D1"


def test_write_daily_data_returns_worksheet():
    """The function returns the worksheet for caller convenience."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_ws(workbook)
    period = MonthPeriod(year=2026, month=4)

    result = sheets.write_daily_data(workbook, period, df)

    assert result is fake_ws


# ---------------------------------------------------------------------------
# write_daily_data — integration test against the real workbook
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_daily_data_to_real_workbook(_temp_tab, monkeypatch):
    """End-to-end: write a small Daily Data set to a temp tab, read back, verify cells."""
    df = pd.DataFrame({
        "Date": [date(2026, 4, 1), date(2026, 4, 2)],
        "Delegate": ["TestDelegateA", "TestDelegateB"],
        "Total Delegation": [123.45, 678.90],
        "Rank": [1, 2],
    })

    # Force the writer to use our temp tab name instead of the real one
    monkeypatch.setattr(sheets, "DAILY_DATA_TAB_TITLE", _temp_tab)

    workbook = sheets.get_workbook()
    period = MonthPeriod(year=2026, month=4)
    ws = sheets.write_daily_data(workbook, period, df)

    # Read back: header in row 1, data in rows 2-3
    assert ws.acell("A1").value == "Date"
    assert ws.acell("B1").value == "Delegate"
    assert ws.acell("C1").value == "Total Delegation"
    assert ws.acell("D1").value == "Rank"
    assert ws.acell("A2").value == "2026-04-01"
    assert ws.acell("B2").value == "TestDelegateA"
    c2 = ws.acell("C2").value
    assert c2 is not None
    assert float(c2) == 123.45
    d2 = ws.acell("D2").value
    assert d2 is not None
    assert int(d2) == 1


# ---------------------------------------------------------------------------
# write_participation_raw_data — Participation Raw Data tab writer
# ---------------------------------------------------------------------------


def _make_participation_df():
    """Pre-transpose participation df: one row per delegate, one column per poll/spell."""
    return pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky", "BONAPUBLICA"],
        "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
        "Start Date": ["2025-12-01", "2025-12-01", "2025-12-01"],
        # Two polls + one spell
        "12345": ["Yes", "No", "Yes"],
        "12346": ["Yes", "Yes", "Pending verification"],
        "0xspell001": ["Yes", "No Delegated SKY", "Yes"],
    })


def _make_poll_info():
    """Sample poll_info, two polls covering 12345 and 12346."""
    return [
        {
            "pollId": 12345,
            "title": "Approve SubDAO X",
            "startDate": date(2026, 4, 5),
            "endDate": date(2026, 4, 8),
        },
        {
            "pollId": 12346,
            "title": "Adjust risk parameter",
            "startDate": date(2026, 4, 15),
            "endDate": date(2026, 4, 18),
        },
    ]


def _make_spell_info():
    """Sample spell_info covering 0xspell001."""
    return [
        {
            "address": "0xspell001",
            "title": "Spell: April risk adjustment",
            "startDate": date(2026, 4, 20),
            "endDate": date(2026, 4, 22),
        },
    ]


def test_build_participation_values_returns_header_plus_one_row_per_poll():
    """Header row first, then one row per poll/spell column in df."""
    df = _make_participation_df()
    values = sheets.build_participation_values(df, _make_poll_info(), _make_spell_info())

    # 1 header + 3 poll/spell rows (12345, 12346, 0xspell001)
    assert len(values) == 4


def test_build_participation_values_header_format():
    """Header is metadata columns + delegate names in df row order."""
    df = _make_participation_df()
    values = sheets.build_participation_values(df, _make_poll_info(), _make_spell_info())

    assert values[0] == ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky", "BONAPUBLICA"]


def test_build_participation_values_row_format():
    """Each non-header row: poll_id, start_date, end_date, title, then per-delegate statuses."""
    df = _make_participation_df()
    values = sheets.build_participation_values(df, _make_poll_info(), _make_spell_info())

    # First poll row: 12345
    assert values[1] == ["12345", "2026-04-05", "2026-04-08", "Approve SubDAO X", "Yes", "No", "Yes"]


def test_build_participation_values_spell_has_empty_end_date():
    """Spell rows have blank End Date in the values matrix (spells lack endDate)."""
    df = _make_participation_df()
    # Drop endDate from spell_info to confirm it surfaces as ""
    spell_info = [
        {
            "address": "0xspell001",
            "title": "Spell: April risk adjustment",
            "startDate": date(2026, 4, 20),
        }
    ]
    values = sheets.build_participation_values(df, _make_poll_info(), spell_info)

    # Spell row (third data row) should have empty End Date.
    spell_row = values[3]
    assert spell_row[0] == "0xspell001"
    assert spell_row[2] == ""  # End Date


def test_build_participation_values_unknown_column_has_blank_metadata():
    """Poll/spell column in df with no matching info: metadata cells blank, statuses still written."""
    df = pd.DataFrame({
        "Delegate Name": ["Alice"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "99999": ["Yes"],  # not in poll_info or spell_info
    })
    values = sheets.build_participation_values(df, [], [])

    # Header + 1 row for the unknown column
    assert len(values) == 2
    assert values[1] == ["99999", "", "", "", "Yes"]


def test_build_participation_values_zero_polls_returns_header_only():
    """Zero-poll month: only the header row, no data rows."""
    df = pd.DataFrame({
        "Delegate Name": ["Alice"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
    })
    values = sheets.build_participation_values(df, [], [])

    assert len(values) == 1
    assert values[0] == ["Poll Id", "Start Date", "End Date", "Title", "Alice"]


def test_participation_metadata_columns_pinned():
    """The metadata columns are pinned so an edit fails the test."""
    assert sheets.PARTICIPATION_METADATA_COLUMNS == ("Poll Id", "Start Date", "End Date", "Title")


def test_participation_raw_data_tab_title():
    period = MonthPeriod(year=2026, month=4)
    assert sheets._participation_raw_data_tab_title(period) == ("Participation Raw Data April 2026")


def test_lookup_poll_or_spell_finds_poll():
    """Identifier matches a poll's pollId (as string)."""
    poll_info = _make_poll_info()
    spell_info = _make_spell_info()
    result = sheets._lookup_poll_or_spell("12345", poll_info, spell_info)
    assert result is not None
    assert result["title"] == "Approve SubDAO X"


def test_lookup_poll_or_spell_finds_spell():
    """Identifier matches a spell's address."""
    poll_info = _make_poll_info()
    spell_info = _make_spell_info()
    result = sheets._lookup_poll_or_spell("0xspell001", poll_info, spell_info)
    assert result is not None
    assert result["title"] == "Spell: April risk adjustment"


def test_lookup_poll_or_spell_returns_none_when_missing():
    """Unknown identifier returns None — caller decides how to handle."""
    poll_info = _make_poll_info()
    spell_info = _make_spell_info()
    assert sheets._lookup_poll_or_spell("999999", poll_info, spell_info) is None


def test_coerce_date_handles_date():
    assert sheets._coerce_date(date(2026, 4, 5)) == "2026-04-05"


def test_coerce_date_handles_datetime():
    """Datetime → ISO date string, time portion dropped."""
    assert sheets._coerce_date(datetime(2026, 4, 5, 12, 30)) == "2026-04-05"


def test_coerce_date_handles_pandas_timestamp():
    ts = pd.Timestamp("2026-04-05 12:30")
    assert sheets._coerce_date(ts) == "2026-04-05"


def test_coerce_date_handles_none():
    """Return empty string for None (spells have no endDate key)."""
    assert sheets._coerce_date(None) == ""


def test_write_participation_creates_tab_with_correct_title():
    """Tab title is 'Participation Raw Data {period}'."""
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    call = workbook.add_worksheet.call_args
    assert call.kwargs["title"] == "Participation Raw Data April 2026"


def test_write_participation_clears_existing_tab_before_writing():
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    fake_ws.clear.assert_called_once()
    fake_ws.update.assert_called_once()


def test_write_participation_header_includes_metadata_and_delegate_names():
    """Header: metadata columns + delegate names in df row order."""
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0] == [
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "BLUE",
        "Cloaky",
        "BONAPUBLICA",
    ]


def test_write_participation_data_rows_one_per_poll_with_metadata():
    """Write one row per poll/spell with metadata + statuses in df column order."""
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    # Header + 3 data rows (2 polls + 1 spell)
    assert len(values) == 4
    # Poll 12345: statuses Yes, No, Yes
    assert values[1] == [
        "12345",
        "2026-04-05",
        "2026-04-08",
        "Approve SubDAO X",
        "Yes",
        "No",
        "Yes",
    ]
    # Poll 12346: statuses Yes, Yes, Pending verification
    assert values[2] == [
        "12346",
        "2026-04-15",
        "2026-04-18",
        "Adjust risk parameter",
        "Yes",
        "Yes",
        "Pending verification",
    ]
    # Spell 0xspell001: statuses Yes, No Delegated SKY, Yes
    assert values[3] == [
        "0xspell001",
        "2026-04-20",
        "2026-04-22",
        "Spell: April risk adjustment",
        "Yes",
        "No Delegated SKY",
        "Yes",
    ]


def test_write_participation_unknown_poll_id_has_blank_metadata():
    """Unknown poll/spell column gets blank metadata; status data is still written."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "99999": ["Yes"],  # poll ID not in poll_info
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[1] == ["99999", "", "", "", "Yes"]


def test_write_participation_zero_polls_writes_header_only():
    """A delegate-only df with no poll/spell columns writes just the header."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        [],
        [],
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values == [["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"]]


def test_write_participation_returns_worksheet():
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    result = sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    assert result is fake_ws


def test_write_participation_range_matches_data_shape():
    """The A1 range matches the values shape: rows x cols."""
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    # 7 columns (4 metadata + 3 delegates), 4 rows (header + 2 polls + 1 spell)
    assert fake_ws.update.call_args.kwargs["range_name"] == "A1:G4"


# ---------------------------------------------------------------------------
# write_participation_raw_data — integration test against the real workbook
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_participation_raw_data_to_real_workbook(_temp_tab, monkeypatch):
    """End-to-end: write a small participation set, read back, verify."""
    df = _make_participation_df()
    monkeypatch.setattr(
        sheets,
        "_participation_raw_data_tab_title",
        lambda period: _temp_tab,
    )

    workbook = sheets.get_workbook()
    period = MonthPeriod(year=2026, month=4)
    ws = sheets.write_participation_raw_data(
        workbook,
        period,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    # Header in row 1
    assert ws.acell("A1").value == "Poll Id"
    assert ws.acell("D1").value == "Title"
    assert ws.acell("E1").value == "BLUE"
    # First data row: poll 12345
    assert ws.acell("A2").value == "12345"
    assert ws.acell("B2").value == "2026-04-05"
    assert ws.acell("D2").value == "Approve SubDAO X"
    assert ws.acell("E2").value == "Yes"
    assert ws.acell("F2").value == "No"


# ---------------------------------------------------------------------------
# write_communication_master — Communication Master tab writer
# ---------------------------------------------------------------------------


def _empty_existing_comm_ws(workbook):
    """Make a fresh MagicMock worksheet for first-fetch Communication Master."""
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = []
    workbook.worksheet.return_value = fake_ws
    return fake_ws


def test_communication_master_tab_title_constant():
    """Workbook-wide tab name."""
    assert sheets.COMMUNICATION_MASTER_TAB_TITLE == "Communication Master"


def test_communication_master_pending_default_constant():
    """The default for new cells where the operator needs to review."""
    assert sheets.COMMUNICATION_PENDING_DEFAULT == "Pending verification"


def test_isblank_true_for_empty_none_whitespace():
    assert sheets._isblank("") is True
    assert sheets._isblank(None) is True
    assert sheets._isblank("   ") is True
    assert sheets._isblank("\t\n") is True


def test_isblank_false_for_real_values():
    assert sheets._isblank("Yes") is False
    assert sheets._isblank("Pending verification") is False
    assert sheets._isblank("Did not vote") is False


def test_write_communication_master_missing_df_column_raises():
    """Raise when the df has no 'Delegate Name' column."""
    df_bad = pd.DataFrame({"NotTheRightColumn": ["foo"]})
    workbook = MagicMock()

    with pytest.raises(ValueError, match="Delegate Name"):
        sheets.write_communication_master(
            workbook,
            df_bad,
            _make_poll_info(),
            _make_spell_info(),
        )


def test_write_communication_master_first_fetch_creates_header():
    """First fetch (empty tab): header is metadata columns + delegate names in df order."""
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0] == [
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "BLUE",
        "Cloaky",
        "BONAPUBLICA",
    ]


def test_write_communication_master_first_fetch_pending_for_yes_participation():
    """First fetch: 'Yes' participation defaults communication to 'Pending verification'."""
    # Fixture: poll 12345 has all 3 delegates as "Yes" - except cloaky is "No"
    # Use a simplified df where everyone said Yes
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
        "12345": ["Yes", "Yes"],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    # Row for poll 12345: metadata + Pending verification for both delegates
    assert len(values) == 2
    assert values[1][4] == "Pending verification"  # BLUE
    assert values[1][5] == "Pending verification"  # Cloaky


def test_write_communication_master_first_fetch_did_not_vote_for_no_participation():
    """Participation = 'No' → communication = 'Did not vote' (cross-ref)."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
        "12345": ["Yes", "No"],  # Cloaky didn't vote
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[1][4] == "Pending verification"  # BLUE (Yes → needs review)
    assert values[1][5] == "Did not vote"  # Cloaky (No → cross-ref)


def test_write_communication_master_first_fetch_mirrors_discounted():
    """Participation in DISCOUNTED → communication mirrors that status."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky", "BONAPUBLICA"],
        "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
        "Start Date": ["2025-12-01", "2025-12-01", "2025-12-01"],
        "12345": ["Not Started", "Exited", "No Delegated SKY"],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[1][4] == "Not Started"
    assert values[1][5] == "Exited"
    assert values[1][6] == "No Delegated SKY"


def test_write_communication_master_missing_column_raises():
    """Raise with clear instructions when YAML adds a delegate not in the existing tab."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky", "NewDelegate"],  # NewDelegate added
        "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
        "Start Date": ["2025-12-01", "2025-12-01", "2026-04-01"],
        "12345": ["Yes", "Yes", "Yes"],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    # Existing tab has only BLUE and Cloaky columns
    fake_ws.get_all_values.return_value = [
        ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"],
        ["12344", "2026-03-01", "2026-03-04", "Old poll", "Yes", "No"],
    ]
    workbook.worksheet.return_value = fake_ws

    with pytest.raises(ValueError, match="NewDelegate"):
        sheets.write_communication_master(
            workbook,
            df,
            _make_poll_info(),
            _make_spell_info(),
        )


def test_write_communication_master_preserves_operator_edits():
    """Preserve existing non-blank cells; fill blanks with the cross-reference default."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
        "12345": ["Yes", "Yes"],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    # Existing tab has poll 12345 with operator-set "Yes" for BLUE,
    # and blank for Cloaky
    fake_ws.get_all_values.return_value = [
        ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"],
        ["12345", "2026-04-05", "2026-04-08", "Approve SubDAO X", "Yes", ""],
    ]
    workbook.worksheet.return_value = fake_ws

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    # Find the poll 12345 row
    row_12345 = next(r for r in values[1:] if r[0] == "12345")
    assert row_12345[4] == "Yes"  # BLUE — operator edit preserved
    assert row_12345[5] == "Pending verification"  # Cloaky — was blank, now default


def test_write_communication_master_preserves_historical_polls():
    """Polls in the existing tab but not in the current fetch are kept."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
        "12345": ["Yes", "Yes"],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"],
        ["12340", "2026-03-01", "2026-03-04", "March poll", "Yes", "No"],
        ["12341", "2026-03-15", "2026-03-18", "Another March poll", "No", "Yes"],
    ]
    workbook.worksheet.return_value = fake_ws

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    poll_ids = [r[0] for r in values[1:]]
    # All three polls present: two historical + one new
    assert "12340" in poll_ids
    assert "12341" in poll_ids
    assert "12345" in poll_ids


def test_write_communication_master_historical_delegate_cells_blank_for_new_polls():
    """Preserve historical delegate columns but leave new poll rows blank for them.

    A delegate removed from YAML keeps their column (historical data)
    but we don't know their alignment dates for new polls.
    """
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],  # Cloaky no longer in roster
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "12345": ["Yes"],
    })
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = [
        ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"],
        # Existing historical poll with operator-set values for both
        ["12340", "2026-03-01", "2026-03-04", "March poll", "Yes", "No"],
    ]
    workbook.worksheet.return_value = fake_ws

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    # Find the new poll row (12345)
    row_12345 = next(r for r in values[1:] if r[0] == "12345")
    assert row_12345[4] == "Pending verification"  # BLUE (in roster, Yes participation)
    # Cloaky column at index 5: blank since not in current roster
    assert row_12345[5] == ""


def test_write_communication_master_sort_order_start_date_descending():
    """Output sorted by Start Date descending (newest first)."""
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "12345": ["Yes"],
        "12346": ["Yes"],
    })
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    values = fake_ws.update.call_args.kwargs["values"]
    # Poll 12346 starts April 15; poll 12345 starts April 5 — 12346 first
    poll_ids = [r[0] for r in values[1:]]
    assert poll_ids[0] == "12346"
    assert poll_ids[1] == "12345"


def test_write_communication_master_clears_before_writing():
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    fake_ws.clear.assert_called_once()
    fake_ws.update.assert_called_once()


def test_write_communication_master_returns_worksheet():
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = _empty_existing_comm_ws(workbook)

    result = sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    assert result is fake_ws


def test_write_communication_master_uses_correct_tab_name():
    df = _make_participation_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    fake_ws.get_all_values.return_value = []
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws

    sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    call = workbook.add_worksheet.call_args
    assert call.kwargs["title"] == "Communication Master"


# ---------------------------------------------------------------------------
# write_communication_master — integration test against the real workbook
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_communication_master_to_real_workbook(_temp_tab, monkeypatch):
    """End-to-end: first-fetch write, read back, verify defaults are applied."""
    df = _make_participation_df()
    monkeypatch.setattr(
        sheets,
        "COMMUNICATION_MASTER_TAB_TITLE",
        _temp_tab,
    )

    workbook = sheets.get_workbook()
    ws = sheets.write_communication_master(
        workbook,
        df,
        _make_poll_info(),
        _make_spell_info(),
    )

    # Header
    assert ws.acell("A1").value == "Poll Id"
    assert ws.acell("E1").value == "BLUE"
    # Spell 0xspell001 sorts first — startDate April 20 is latest among the
    # fixture (poll 12346 starts Apr 15, poll 12345 starts Apr 5).
    assert ws.acell("A2").value == "0xspell001"
    # BLUE's communication cell for the spell. Fixture participation column
    # has "Yes" for BLUE on the spell, so cross-ref defaults to Pending
    # verification.
    assert ws.acell("E2").value == "Pending verification"  # BLUE


# ---------------------------------------------------------------------------
# Config tab — read_config
# ---------------------------------------------------------------------------


def _make_config_ws(rows: list[list[str]]) -> MagicMock:
    """Build a MagicMock worksheet returning `rows` from get_all_values()."""
    ws = MagicMock(spec=gspread.Worksheet)
    ws.get_all_values.return_value = rows
    return ws


def test_read_config_missing_tab_raises_runtime_error():
    """Hard fail with operator-friendly message when Config tab is absent."""
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("Config")

    with pytest.raises(RuntimeError, match=r"Config.*tab"):
        sheets.read_config(workbook)


def test_read_config_empty_tab_raises_value_error():
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([])

    with pytest.raises(ValueError, match="empty"):
        sheets.read_config(workbook)


def test_read_config_missing_required_key_raises():
    """ValueError lists the missing keys for the operator."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["L2_USDS", "14583"],
        # L3_USDS and TOTAL_SLOTS missing
    ])

    with pytest.raises(ValueError, match="missing required keys"):
        sheets.read_config(workbook)


def test_read_config_unparseable_value_raises():
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["L1_USDS", "not_a_number"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])

    with pytest.raises(ValueError, match="un-parseable"):
        sheets.read_config(workbook)


def test_read_config_happy_path_returns_compensation_config():
    from ad_voting_metrics.compensation import CompensationConfig

    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])

    config = sheets.read_config(workbook)
    assert isinstance(config, CompensationConfig)
    assert config.l1_usds == 33333.0
    assert config.l2_usds == 14583.0
    assert config.l3_usds == 4000.0
    assert config.total_slots == 6


def test_read_config_unknown_keys_silently_ignored():
    """Future-proofing: extra keys in the Config tab don't break the read."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
        ["FUTURE_THRESHOLD", "0.5"],  # not yet defined
        ["BUFFER_CAP", "100000"],  # not yet defined
    ])

    config = sheets.read_config(workbook)
    assert config.l1_usds == 33333.0


def test_read_config_handles_short_rows():
    """A row with only one cell (no value column) is skipped."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["accidentally_partial_row"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])

    config = sheets.read_config(workbook)
    assert config.l1_usds == 33333.0


def test_read_config_strips_whitespace_from_keys_and_values():
    workbook = MagicMock()
    workbook.worksheet.return_value = _make_config_ws([
        ["Key", "Value"],
        ["  L1_USDS  ", "  33333  "],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])

    config = sheets.read_config(workbook)
    assert config.l1_usds == 33333.0


# ---------------------------------------------------------------------------
# Compensation tab — write_compensation_tab
# ---------------------------------------------------------------------------


def _make_period_comp(
    *,
    delegates: list[tuple[str, int | None, float | None, float | None, int]] | None = None,
    validation: dict[str, str] | None = None,
):
    """Build a PeriodCompensation for testing.

    Each delegate tuple is (name, level_at_period_end, p_pct, c_pct, days_l3).
    Modifier and final_amount are derived. Defaults to a single Alice row.
    """
    from ad_voting_metrics.compensation import (
        CompensationConfig,
        DelegateCompensation,
        PeriodCompensation,
        component_modifier,
    )

    if delegates is None:
        delegates = [("Alice", 3, 1.0, 1.0, 30)]

    config = CompensationConfig(
        l1_usds=33333.0,
        l2_usds=14583.0,
        l3_usds=4000.0,
        total_slots=6,
    )
    period = MonthPeriod(year=2026, month=4)
    rows = []
    for name, level, p_pct, c_pct, days_l3 in delegates:
        modifier = component_modifier(p_pct) * component_modifier(c_pct)
        entitlement = (days_l3 / 30) * config.l3_usds
        final_amount = round(entitlement * modifier, 0)
        rows.append(
            DelegateCompensation(
                name=name,
                rank_at_period_end=1,
                level_at_period_end=level,
                days_as_l1=0,
                days_as_l2=0,
                days_as_l3=days_l3,
                participation_pct=p_pct,
                communication_pct=c_pct,
                metrics_modifier=modifier,
                entitlement_pre_modifier=entitlement,
                final_amount=final_amount,
                buffer_carry_in=0.0,
                buffer_added=final_amount,
                payment_amount=0.0,
                buffer_post_payment=0.0,
                notes="",
            )
        )
    return PeriodCompensation(
        period=period,
        config=config,
        days_in_period=30,
        per_delegate=rows,
        validation=validation or {"slot_days_check": "GOOD"},
    )


def test_write_compensation_tab_uses_correct_tab_name():
    """Tab is named '{Month Year} Compensation'."""
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, _make_period_comp())

    workbook.worksheet.assert_called_with("April 2026 Compensation")


def test_write_compensation_tab_writes_header_block():
    """Header block in rows 1-5 contains period metadata."""
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, _make_period_comp())

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0][0] == "Year"
    assert values[0][1] == 2026
    assert values[1][0] == "Month"
    assert values[1][1] == "April"
    assert values[2][0] == "Period Start"
    assert values[2][1] == "2026-04-01"
    assert values[3][0] == "Period End"
    assert values[3][1] == "2026-04-30"
    assert values[4][0] == "Days in Month"
    assert values[4][1] == 30


def test_write_compensation_tab_writes_config_reference_amounts():
    """Columns D-E rows 1-3 carry the config USDS amounts."""
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, _make_period_comp())

    values = fake_ws.update.call_args.kwargs["values"]
    # Row 1: D="Level 1 USDS", E=33333
    assert values[0][3] == "Level 1 USDS"
    assert values[0][4] == 33333.0
    assert values[1][3] == "Level 2 USDS"
    assert values[1][4] == 14583.0
    assert values[2][3] == "Level 3 USDS"
    assert values[2][4] == 4000.0


def test_write_compensation_tab_writes_level_counts():
    """Columns G-H rows 1-3 carry counts of delegates at each level."""
    period_comp = _make_period_comp(
        delegates=[
            ("Alice", 1, 1.0, 1.0, 0),
            ("Bob", 1, 1.0, 1.0, 0),
            ("Charlie", 2, 1.0, 1.0, 0),
            ("Dave", 3, 1.0, 1.0, 30),
            ("Eve", 3, 1.0, 1.0, 30),
            ("Frank", None, 0.5, 1.0, 0),  # unassigned, doesn't count
        ]
    )

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0][6] == "Number of Level 1"
    assert values[0][7] == 2
    assert values[1][7] == 1
    assert values[2][7] == 2


def test_write_compensation_tab_writes_total_sum_formula():
    """Row 7 has 'Total Final Amount' and SUM(H10:H...) formula."""
    period_comp = _make_period_comp(
        delegates=[
            ("Alice", 3, 1.0, 1.0, 30),
            ("Bob", 3, 1.0, 1.0, 30),
        ]
    )

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[6][0] == "Total Final Amount"
    # 2 data rows: rows 10 and 11. SUM(H10:H11).
    assert values[6][1] == "=SUM(H10:H11)"


def test_write_compensation_tab_writes_slot_days_check():
    """Row 8 carries the slot_days_check validation status."""
    pc = _make_period_comp(validation={"slot_days_check": "NOT GOOD"})

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, pc)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[7][0] == "Slot Days Check"
    assert values[7][1] == "NOT GOOD"


def test_write_compensation_tab_writes_column_headers_at_row_9():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, _make_period_comp())

    values = fake_ws.update.call_args.kwargs["values"]
    # Row 9 (index 8) is the column header row.
    assert values[8] == list(sheets.COMPENSATION_COLUMNS)


def test_write_compensation_tab_writes_data_rows_in_order():
    """Data rows preserve the per_delegate order (alphabetical, set by computer)."""
    period_comp = _make_period_comp(
        delegates=[
            ("Alice", 3, 1.0, 1.0, 30),
            ("Bob", 3, 1.0, 1.0, 30),
            ("Charlie", 3, 1.0, 1.0, 30),
        ]
    )

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    # Row 10 (index 9) first data row, Delegate column (A, index 0).
    assert values[9][0] == "Alice"
    assert values[10][0] == "Bob"
    assert values[11][0] == "Charlie"


def test_write_compensation_tab_none_pct_renders_as_no_data():
    """A delegate with None participation_pct shows 'No Data' in the cell."""
    period_comp = _make_period_comp(
        delegates=[
            ("Newbie", None, None, None, 0),
        ]
    )

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    # Row 10 first data row; B = participation 6mo, C = communication 6mo.
    assert values[9][1] == "No Data"
    assert values[9][2] == "No Data"


def test_write_compensation_tab_level_label_mapping():
    """level_at_period_end maps to 'Level N' or 'No' in column E."""
    period_comp = _make_period_comp(
        delegates=[
            ("A_L1", 1, 1.0, 1.0, 0),
            ("B_L2", 2, 1.0, 1.0, 0),
            ("C_L3", 3, 1.0, 1.0, 30),
            ("D_None", None, 0.5, 1.0, 0),
        ]
    )

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    # Column E (index 4) = "Ranked During Month?".
    assert values[9][4] == "Level 1"
    assert values[10][4] == "Level 2"
    assert values[11][4] == "Level 3"
    assert values[12][4] == "No"


def test_write_compensation_tab_empty_per_delegate_still_writes_header():
    """No delegates → header block + column header row only, no error."""
    period_comp = _make_period_comp(delegates=[])

    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, period_comp)

    values = fake_ws.update.call_args.kwargs["values"]
    # 8 header-block rows + 1 column-header row = 9 rows total.
    assert len(values) == 9
    assert values[8] == list(sheets.COMPENSATION_COLUMNS)
    # SUM formula points to H10 even with no data rows.
    assert values[6][1] == "=SUM(H10:H10)"


def test_write_compensation_tab_clears_before_writing():
    """clear_tab is called before update — re-runs replace, don't merge."""
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    sheets.write_compensation_tab(workbook, _make_period_comp())

    fake_ws.clear.assert_called_once()
    # Order matters: clear before update.
    clear_idx = fake_ws.method_calls.index(next(c for c in fake_ws.method_calls if c[0] == "clear"))
    update_idx = fake_ws.method_calls.index(
        next(c for c in fake_ws.method_calls if c[0] == "update")
    )
    assert clear_idx < update_idx


def test_write_compensation_tab_returns_worksheet():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    result = sheets.write_compensation_tab(workbook, _make_period_comp())

    assert result is fake_ws


def test_compensation_columns_has_14_columns():
    """Pin the column count; accidental additions fail the test."""
    assert len(sheets.COMPENSATION_COLUMNS) == 14


# ---------------------------------------------------------------------------
# _enumerate_months
# ---------------------------------------------------------------------------


def test_enumerate_months_single_month():
    """Start and end in the same month → one MonthPeriod."""
    months = sheets._enumerate_months(date(2026, 4, 1), date(2026, 4, 30))
    assert months == [MonthPeriod(year=2026, month=4)]


def test_enumerate_months_window_spans_year_rollover():
    """November 2025 through April 2026 = 6 months including rollover."""
    months = sheets._enumerate_months(date(2025, 11, 1), date(2026, 4, 30))
    assert months == [
        MonthPeriod(year=2025, month=11),
        MonthPeriod(year=2025, month=12),
        MonthPeriod(year=2026, month=1),
        MonthPeriod(year=2026, month=2),
        MonthPeriod(year=2026, month=3),
        MonthPeriod(year=2026, month=4),
    ]


def test_enumerate_months_partial_month_at_edges():
    """Mid-month dates still resolve to the containing months."""
    months = sheets._enumerate_months(date(2025, 11, 15), date(2026, 4, 10))
    assert len(months) == 6


# ---------------------------------------------------------------------------
# read_daily_data
# ---------------------------------------------------------------------------


def _daily_data_ws(rows: list[list[str]]) -> MagicMock:
    """Build a MagicMock Daily Data worksheet from row data."""
    ws = MagicMock(spec=gspread.Worksheet)
    ws.get_all_values.return_value = rows
    return ws


def test_read_daily_data_missing_tab_raises_with_operator_message():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("Daily Data")

    with pytest.raises(RuntimeError, match=r"Daily Data.*tab.*fetch"):
        sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))


def test_read_daily_data_no_rows_in_period_raises():
    """Case A: tab exists but has no rows covering the requested period."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _daily_data_ws([
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-01-15", "Alice", "1000000", "1"],  # different period
    ])

    with pytest.raises(RuntimeError, match="no rows for"):
        sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))


def test_read_daily_data_returns_ranks_for_every_day_present():
    """Pivots rows into {day: {delegate: rank}}."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _daily_data_ws([
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-04-01", "Alice", "1000000", "1"],
        ["2026-04-01", "Bob", "500000", "2"],
        ["2026-04-02", "Alice", "1000000", "1"],
        ["2026-04-02", "Bob", "500000", "2"],
    ])

    result = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))

    assert result[date(2026, 4, 1)] == {"Alice": 1, "Bob": 2}
    assert result[date(2026, 4, 2)] == {"Alice": 1, "Bob": 2}


def test_read_daily_data_filters_out_other_months():
    """Workbook-wide tab; only rows in the requested period come back."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _daily_data_ws([
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["2026-03-31", "Alice", "1000000", "1"],  # March
        ["2026-04-01", "Alice", "1000000", "1"],  # April — kept
        ["2026-05-01", "Alice", "1000000", "1"],  # May — filtered
    ])

    result = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))

    assert list(result.keys()) == [date(2026, 4, 1)]


def test_read_daily_data_skips_unparseable_dates():
    """Malformed date strings are dropped, not raised."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _daily_data_ws([
        ["Date", "Delegate", "Total Delegation", "Rank"],
        ["not-a-date", "Alice", "1000000", "1"],
        ["2026-04-01", "Alice", "1000000", "1"],
    ])

    result = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))

    assert result == {date(2026, 4, 1): {"Alice": 1}}


# ---------------------------------------------------------------------------
# read_participation_for_window
# ---------------------------------------------------------------------------


def _participation_ws(rows: list[list[str]]) -> MagicMock:
    """Build a MagicMock Participation Raw Data worksheet from row data."""
    ws = MagicMock(spec=gspread.Worksheet)
    ws.get_all_values.return_value = rows
    return ws


def test_read_participation_for_window_missing_tabs_skipped_silently():
    """Months with no Participation Raw Data tab contribute nothing."""
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("any")

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2025, 11, 1),
        window_end=date(2026, 4, 30),
    )

    assert result == {}


def test_read_participation_for_window_aggregates_across_months():
    """Polls from multiple monthly tabs aggregate into one per-delegate list."""
    workbook = MagicMock()

    def by_title(title: str) -> MagicMock:
        if title == "Participation Raw Data March 2026":
            return _participation_ws([
                ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
                ["1001", "2026-03-15", "2026-03-20", "Poll 1", "Yes", "Yes"],
            ])
        if title == "Participation Raw Data April 2026":
            return _participation_ws([
                ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
                ["1002", "2026-04-05", "2026-04-10", "Poll 2", "Yes", "No"],
                ["1003", "2026-04-15", "2026-04-20", "Poll 3", "No", "Yes"],
            ])
        raise gspread.exceptions.WorksheetNotFound(title)

    workbook.worksheet.side_effect = by_title

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 3, 1),
        window_end=date(2026, 4, 30),
    )

    assert result == {
        "Alice": [
            ("1001", date(2026, 3, 15), "Yes"),
            ("1002", date(2026, 4, 5), "Yes"),
            ("1003", date(2026, 4, 15), "No"),
        ],
        "Bob": [
            ("1001", date(2026, 3, 15), "Yes"),
            ("1002", date(2026, 4, 5), "No"),
            ("1003", date(2026, 4, 15), "Yes"),
        ],
    }


def test_read_participation_for_window_filters_polls_outside_window():
    """A poll dated outside the window bounds is dropped even if its tab is in scope."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _participation_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice"],
        ["1001", "2026-04-01", "2026-04-10", "In window", "Yes"],
        ["1002", "2026-04-30", "2026-05-05", "Out of window", "No"],
    ])

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 15),  # cutoff before poll 1002
    )

    assert result == {"Alice": [("1001", date(2026, 4, 1), "Yes")]}


def test_read_participation_for_window_skips_rows_with_unparseable_dates():
    workbook = MagicMock()
    workbook.worksheet.return_value = _participation_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice"],
        ["1001", "not-a-date", "2026-04-10", "Broken", "Yes"],
        ["1002", "2026-04-05", "2026-04-10", "OK", "Yes"],
    ])

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 30),
    )

    assert result == {"Alice": [("1002", date(2026, 4, 5), "Yes")]}


def test_read_participation_for_window_handles_empty_tab():
    workbook = MagicMock()
    workbook.worksheet.return_value = _participation_ws([])

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 30),
    )

    assert result == {}


def test_read_participation_for_window_header_only_tab():
    """Header without data rows produces empty lists per delegate."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _participation_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
    ])

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 30),
    )

    # The function aggregates only delegates that had at least one poll;
    # a header-only tab adds no entries.
    assert result == {}


def test_read_participation_for_window_partial_rows():
    """A row missing trailing status cells gets empty string for those delegates."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _participation_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
        ["1001", "2026-04-05", "2026-04-10", "Poll", "Yes"],  # missing Bob's column
    ])

    result = sheets.read_participation_for_window(
        workbook,
        window_start=date(2026, 4, 1),
        window_end=date(2026, 4, 30),
    )

    assert result["Alice"] == [("1001", date(2026, 4, 5), "Yes")]
    assert result["Bob"] == [("1001", date(2026, 4, 5), "")]


# ---------------------------------------------------------------------------
# read_communication_master
# ---------------------------------------------------------------------------


def _comm_master_ws(rows: list[list[str]]) -> MagicMock:
    ws = MagicMock(spec=gspread.Worksheet)
    ws.get_all_values.return_value = rows
    return ws


def test_read_communication_master_missing_tab_raises():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound("Communication Master")

    with pytest.raises(RuntimeError, match=r"Communication Master.*fetch"):
        sheets.read_communication_master(workbook)


def test_read_communication_master_empty_tab_returns_empty_dict():
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([])

    assert sheets.read_communication_master(workbook) == {}


def test_read_communication_master_header_only_returns_empty():
    """Header but no data rows → empty dict (no entries to populate)."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
    ])

    assert sheets.read_communication_master(workbook) == {}


def test_read_communication_master_pivots_into_delegate_keyed_dict():
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
        ["1001", "2026-04-05", "2026-04-10", "Poll 1", "Yes", "No"],
        ["1002", "2026-04-15", "2026-04-20", "Poll 2", "Pending verification", "Yes"],
    ])

    result = sheets.read_communication_master(workbook)

    assert result == {
        "Alice": {"1001": "Yes", "1002": "Pending verification"},
        "Bob": {"1001": "No", "1002": "Yes"},
    }


def test_read_communication_master_skips_blank_poll_id_rows():
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice"],
        ["1001", "2026-04-05", "2026-04-10", "Poll 1", "Yes"],
        ["", "", "", "", ""],  # blank row
        ["1002", "2026-04-15", "2026-04-20", "Poll 2", "No"],
    ])

    result = sheets.read_communication_master(workbook)

    assert result == {"Alice": {"1001": "Yes", "1002": "No"}}


def test_read_communication_master_partial_rows_get_empty_string():
    """A row missing trailing cells gets empty string for those delegates."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([
        ["Poll Id", "Start Date", "End Date", "Title", "Alice", "Bob"],
        ["1001", "2026-04-05", "2026-04-10", "Poll 1", "Yes"],  # missing Bob
    ])

    result = sheets.read_communication_master(workbook)

    assert result["Alice"] == {"1001": "Yes"}
    assert result["Bob"] == {"1001": ""}


def test_read_communication_master_no_delegate_columns_returns_empty():
    """A tab with only metadata columns (no delegates) returns {}."""
    workbook = MagicMock()
    workbook.worksheet.return_value = _comm_master_ws([
        ["Poll Id", "Start Date", "End Date", "Title"],
        ["1001", "2026-04-05", "2026-04-10", "Poll 1"],
    ])

    assert sheets.read_communication_master(workbook) == {}
