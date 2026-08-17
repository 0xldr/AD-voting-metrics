"""Tests for the sheets module.

Unit tests mock gspread + gspread_dataframe so they don't hit live Google.
Integration tests (marked @pytest.mark.integration) require real env vars
and run with `pytest -m integration`.
"""

import json
import uuid
import warnings
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import gspread
import numpy as np
import pandas as pd
import pytest

from ad_voting_metrics import sheets
from ad_voting_metrics.period import MonthPeriod

# ---------------------------------------------------------------------------
# SCOPES — pinned so an accidental edit fails the test
# ---------------------------------------------------------------------------


def test_scopes_are_sheets_only():
    """Sheets scope covers cell I/O and open_by_key; no Drive scope, keeping the grant minimal."""
    assert sheets.SCOPES == ("https://www.googleapis.com/auth/spreadsheets",)


# ---------------------------------------------------------------------------
# get_workbook — env var validation
# ---------------------------------------------------------------------------


def test_get_workbook_missing_service_account_env_raises(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.setenv("SHEETS_WORKBOOK_ID", "anything")
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        sheets.get_workbook()


def test_get_workbook_missing_workbook_id_env_raises(monkeypatch, tmp_path):
    fake_sa = tmp_path / "fake.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(fake_sa))
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="SHEETS_WORKBOOK_ID"):
        sheets.get_workbook()


def test_get_workbook_both_env_vars_missing_raises_service_account_first(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        sheets.get_workbook()


# ---------------------------------------------------------------------------
# get_workbook — file validation
# ---------------------------------------------------------------------------


def test_get_workbook_service_account_file_missing_raises(tmp_path):
    missing = tmp_path / "nonexistent.json"
    with pytest.raises(RuntimeError, match="not found"):
        sheets.get_workbook(service_account_file=missing, workbook_id="anything")


def test_get_workbook_service_account_path_is_directory_raises(tmp_path):
    with pytest.raises(RuntimeError, match="not a file"):
        sheets.get_workbook(service_account_file=tmp_path, workbook_id="anything")


# ---------------------------------------------------------------------------
# get_workbook — credentials parsing
# ---------------------------------------------------------------------------


def test_get_workbook_malformed_json_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    with pytest.raises(RuntimeError, match="service-account JSON key"):
        sheets.get_workbook(service_account_file=bad, workbook_id="anything")


def test_get_workbook_wrong_key_type_raises(tmp_path):
    # JSON parses but the key type is wrong (e.g., user creds, not service account).
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"type": "authorized_user"}))
    with pytest.raises(RuntimeError, match="service-account JSON key"):
        sheets.get_workbook(service_account_file=wrong, workbook_id="anything")


# ---------------------------------------------------------------------------
# get_workbook — happy path & API errors
# ---------------------------------------------------------------------------


def _make_fake_sa_file(tmp_path: Path) -> Path:
    """Create a minimal valid-looking service-account JSON file path."""
    p = tmp_path / "sa.json"
    p.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "fake@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        ),
    )
    return p


def test_get_workbook_happy_path_returns_spreadsheet(tmp_path):
    fake_sa = _make_fake_sa_file(tmp_path)
    fake_spreadsheet = MagicMock(spec=gspread.Spreadsheet)
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = fake_spreadsheet

    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=MagicMock()),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
    ):
        result = sheets.get_workbook(service_account_file=fake_sa, workbook_id="WB_ID")

    assert result is fake_spreadsheet
    fake_client.open_by_key.assert_called_once_with("WB_ID")


def test_get_workbook_passes_correct_scopes_to_credentials(tmp_path):
    fake_sa = _make_fake_sa_file(tmp_path)
    with (
        patch.object(sheets.Credentials, "from_service_account_file") as creds_mock,
        patch.object(sheets.gspread, "authorize"),
    ):
        creds_mock.return_value = MagicMock()
        sheets.get_workbook(service_account_file=fake_sa, workbook_id="WB_ID")
    _, kwargs = creds_mock.call_args
    assert tuple(kwargs["scopes"]) == sheets.SCOPES


def test_get_workbook_api_error_on_open_wrapped_with_context(tmp_path):
    fake_sa = _make_fake_sa_file(tmp_path)
    fake_client = MagicMock()
    fake_client.open_by_key.side_effect = gspread.exceptions.APIError(
        MagicMock(status_code=403, json=lambda: {"error": {"code": 403, "message": "boom"}}),
    )
    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=MagicMock()),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
        pytest.raises(RuntimeError, match="Could not open workbook"),
    ):
        sheets.get_workbook(service_account_file=fake_sa, workbook_id="BAD_ID")


def test_get_workbook_api_error_message_includes_sa_email(tmp_path):
    fake_sa = _make_fake_sa_file(tmp_path)
    fake_creds = MagicMock(service_account_email="fake@example.iam.gserviceaccount.com")
    fake_client = MagicMock()
    fake_client.open_by_key.side_effect = gspread.exceptions.APIError(
        MagicMock(status_code=403, json=lambda: {"error": {"code": 403, "message": "boom"}}),
    )
    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=fake_creds),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
        pytest.raises(RuntimeError, match=r"fake@example\.iam\.gserviceaccount\.com"),
    ):
        sheets.get_workbook(service_account_file=fake_sa, workbook_id="WB_ID")


def test_get_workbook_explicit_args_override_env(monkeypatch, tmp_path):
    fake_sa = _make_fake_sa_file(tmp_path)
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", "/should/not/be/used")
    monkeypatch.setenv("SHEETS_WORKBOOK_ID", "ENV_ID")
    fake_client = MagicMock()
    fake_client.open_by_key.return_value = MagicMock(spec=gspread.Spreadsheet)
    with (
        patch.object(sheets.Credentials, "from_service_account_file", return_value=MagicMock()),
        patch.object(sheets.gspread, "authorize", return_value=fake_client),
    ):
        sheets.get_workbook(service_account_file=fake_sa, workbook_id="EXPLICIT_ID")
    fake_client.open_by_key.assert_called_once_with("EXPLICIT_ID")


# ---------------------------------------------------------------------------
# get_workbook — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_workbook_real_credentials_opens_spreadsheet():
    """Smoke test: end-to-end open of the real workbook with real creds."""
    workbook = sheets.get_workbook()
    assert workbook.id


# ---------------------------------------------------------------------------
# get_or_create_tab
# ---------------------------------------------------------------------------


def test_get_or_create_tab_returns_existing_tab():
    workbook = MagicMock()
    existing_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = existing_ws
    result = sheets.get_or_create_tab(workbook, "Daily Data", rows=10, cols=4)
    assert result is existing_ws
    workbook.add_worksheet.assert_not_called()


def test_get_or_create_tab_creates_when_missing():
    workbook = MagicMock()
    created_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = created_ws
    result = sheets.get_or_create_tab(workbook, "New Tab", rows=10, cols=4)
    assert result is created_ws
    workbook.add_worksheet.assert_called_once_with(title="New Tab", rows=10, cols=4)


def test_get_or_create_tab_does_not_resize_existing_tab():
    """Existing tabs keep their dimensions; rows/cols only apply at creation."""
    workbook = MagicMock()
    existing_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = existing_ws
    sheets.get_or_create_tab(workbook, "Daily Data", rows=9999, cols=99)
    # No resize called
    existing_ws.resize.assert_not_called()
    workbook.add_worksheet.assert_not_called()


# ---------------------------------------------------------------------------
# Integration: temp_tab fixture for live-workbook tests
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_tab():
    """Create a uniquely-named temp tab, yield the title, and clean it up on teardown."""
    title = f"_test_{uuid.uuid4().hex[:8]}"
    workbook = sheets.get_workbook()
    workbook.add_worksheet(title=title, rows=20, cols=10)
    try:
        yield title
    finally:
        try:
            workbook.del_worksheet(workbook.worksheet(title))
        except gspread.exceptions.WorksheetNotFound:
            warnings.warn(f"temp_tab {title!r} was already gone at teardown", stacklevel=1)


@pytest.mark.integration
def test_get_or_create_tab_creates_and_finds_real_tab(temp_tab):
    workbook = sheets.get_workbook()
    ws = sheets.get_or_create_tab(workbook, temp_tab, rows=10, cols=4)
    assert ws.title == temp_tab
    # Second call returns the same tab
    ws2 = sheets.get_or_create_tab(workbook, temp_tab, rows=10, cols=4)
    assert ws2.id == ws.id


# ---------------------------------------------------------------------------
# Helper fixtures for sheet I/O tests
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_existing_ws():
    """Return (workbook, worksheet) where workbook.worksheet(...) returns a fresh-looking ws."""
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    return workbook, fake_ws


@pytest.fixture
def sheet_io():
    """Patch get_as_dataframe (defaults to empty) and set_with_dataframe.

    Yields (get_mock, set_mock); tests override `get_mock.return_value` to inject existing-tab state and inspect
    `set_mock.call_args.args[1]` to assert on the DataFrame written.
    """
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe") as get_mock,
        patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock,
    ):
        get_mock.return_value = pd.DataFrame()
        yield get_mock, set_mock


@pytest.fixture(autouse=True)
def backup_dir(tmp_path, monkeypatch):
    """Redirect pre-clear tab backups into the test's tmp dir instead of output_data/.

    Yields the backup directory so tests can assert on written backup files.
    """
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(sheets, "BACKUP_DIR", backup_dir)
    return backup_dir


# ---------------------------------------------------------------------------
# write_daily_data — schema enforcement + merge behavior
# ---------------------------------------------------------------------------


def _make_ranking_df():
    """Two-day ranking df with two delegates per day."""
    return pd.DataFrame(
        {
            "Delegate": ["alpha", "beta", "alpha", "beta"],
            "Date": [date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 2)],
            "Total Delegation": [100.0, 50.0, 90.0, 60.0],
            "Rank": [1, 2, 1, 2],
        }
    )


def test_daily_data_columns_pinned():
    """Lock down the canonical column order."""
    assert sheets.DAILY_DATA_COLUMNS == ("Date", "Delegate", "Total Delegation", "Rank")


def test_write_daily_data_missing_columns_raises():
    workbook = MagicMock()
    df_bad = pd.DataFrame({"Delegate": ["alpha"]})
    with pytest.raises(ValueError, match="missing required columns"):
        sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_bad)


def test_write_daily_data_uses_workbook_wide_tab_name(empty_existing_ws, sheet_io):
    workbook, fake_ws = empty_existing_ws
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    df = _make_ranking_df()

    _, _ = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    workbook.add_worksheet.assert_called_once()
    assert workbook.add_worksheet.call_args.kwargs["title"] == "Daily Data"


def test_write_daily_data_first_fetch_writes_header_and_rows(empty_existing_ws, sheet_io):
    """First fetch (empty existing) writes the canonical column order with all rows."""
    workbook, _ = empty_existing_ws
    df = _make_ranking_df()

    _, set_mock = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    written = set_mock.call_args.args[1]
    assert list(written.columns) == list(sheets.DAILY_DATA_COLUMNS)
    assert len(written) == 4
    # Dates were coerced to ISO strings before write.
    assert set(written["Date"]) == {"2026-04-01", "2026-04-02"}


def test_write_daily_data_leaves_the_callers_frame_untouched(empty_existing_ws, sheet_io):
    """The caller's ranking frame is never written through.

    write_daily_data narrows and retypes its input columns. It relies on copy-on-write to keep those writes off the
    caller's frame rather than taking an eager defensive copy.
    """
    workbook, _ = empty_existing_ws
    df = _make_ranking_df()
    before = df.copy(deep=True)

    _, _ = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    pd.testing.assert_frame_equal(df, before)


def test_write_daily_data_clears_before_writing(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    call_order: list[str] = []
    fake_ws.clear.side_effect = lambda: call_order.append("clear")

    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=pd.DataFrame()),
        patch(
            "ad_voting_metrics.sheets.set_with_dataframe",
            side_effect=lambda *_a, **_kw: call_order.append("set"),
        ),
    ):
        sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), _make_ranking_df())

    assert call_order == ["clear", "set"]


def test_write_daily_data_preserves_existing_rows_for_other_dates(empty_existing_ws, sheet_io):
    """Dates not in the current fetch should keep their existing rows."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-03-31", "2026-03-31"],
            "Delegate": ["alpha", "beta"],
            "Total Delegation": ["100", "50"],
            "Rank": ["1", "2"],
        }
    )
    df_new = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta"],
            "Date": [date(2026, 4, 1), date(2026, 4, 1)],
            "Total Delegation": [200.0, 150.0],
            "Rank": [1, 2],
        }
    )

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    written = set_mock.call_args.args[1]
    assert "2026-03-31" in set(written["Date"])
    assert "2026-04-01" in set(written["Date"])
    assert len(written) == 4


def test_write_daily_data_overwrites_existing_rows_for_current_dates(empty_existing_ws, sheet_io):
    """Re-runs replace any existing rows for dates that appear in the new fetch."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-04-01", "2026-04-01"],
            "Delegate": ["alpha", "beta"],
            "Total Delegation": ["999", "999"],  # stale
            "Rank": ["9", "9"],
        }
    )
    df_new = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta"],
            "Date": [date(2026, 4, 1), date(2026, 4, 1)],
            "Total Delegation": [100.0, 50.0],
            "Rank": [1, 2],
        }
    )

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    written = set_mock.call_args.args[1]
    assert len(written) == 2
    assert set(written["Total Delegation"]) == {100.0, 50.0}
    assert 999 not in set(written["Rank"])


def test_write_daily_data_backs_up_existing_before_clear(empty_existing_ws, sheet_io, backup_dir):
    """Non-empty existing data is saved to a local CSV before the tab is cleared."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-03-31", "2026-03-31"],
            "Delegate": ["alpha", "beta"],
            "Total Delegation": ["100", "50"],
            "Rank": ["1", "2"],
        }
    )

    get_mock, _ = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), _make_ranking_df())

    backups = list(backup_dir.glob("Daily Data_*.csv"))
    assert len(backups) == 1
    backup = pd.read_csv(backups[0])
    assert len(backup) == 2
    assert set(backup["Delegate"]) == {"alpha", "beta"}


def test_write_daily_data_first_write_makes_no_backup(empty_existing_ws, sheet_io, backup_dir):
    """An empty tab has nothing to lose; no backup file is written."""
    workbook, _ = empty_existing_ws

    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), _make_ranking_df())

    assert not backup_dir.exists()


def test_write_daily_data_backup_failure_aborts_clear(empty_existing_ws, sheet_io, tmp_path, monkeypatch):
    """If the backup can't be written, the destructive clear must not happen."""
    workbook, fake_ws = empty_existing_ws
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the backup dir should go")
    monkeypatch.setattr(sheets, "BACKUP_DIR", blocked)
    existing = pd.DataFrame(
        {
            "Date": ["2026-03-31"],
            "Delegate": ["alpha"],
            "Total Delegation": ["100"],
            "Rank": ["1"],
        }
    )

    get_mock, _ = sheet_io
    get_mock.return_value = existing
    with pytest.raises(RuntimeError, match="pre-clear backup"):
        sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), _make_ranking_df())

    fake_ws.clear.assert_not_called()


def test_write_daily_data_raises_on_roster_drift(empty_existing_ws):
    """A date whose delegate-row count changed between fetches is fatal."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-04-01", "2026-04-01", "2026-04-01"],
            "Delegate": ["alpha", "beta", "gamma"],
            "Total Delegation": ["100", "50", "25"],
            "Rank": ["1", "2", "3"],
        }
    )
    df_new = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta"],
            "Date": [date(2026, 4, 1), date(2026, 4, 1)],
            "Total Delegation": [100.0, 50.0],
            "Rank": [1, 2],
        }
    )

    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=existing),
        patch("ad_voting_metrics.sheets.set_with_dataframe"),
        pytest.raises(ValueError, match="Roster drift"),
    ):
        sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)


def test_write_daily_data_no_drift_when_dates_dont_overlap(empty_existing_ws, sheet_io):
    """Per-date row count is only checked for dates appearing in both."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-03-31", "2026-03-31", "2026-03-31"],
            "Delegate": ["alpha", "beta", "gamma"],
            "Total Delegation": ["100", "50", "25"],
            "Rank": ["1", "2", "3"],
        }
    )
    df_new = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta"],
            "Date": [date(2026, 4, 1), date(2026, 4, 1)],
            "Total Delegation": [100.0, 50.0],
            "Rank": [1, 2],
        }
    )

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    # 3 + 2 = 5 rows, no drift error
    assert len(set_mock.call_args.args[1]) == 5


def test_write_daily_data_handles_unparseable_existing_rows(empty_existing_ws, sheet_io):
    """Existing rows with non-numeric Rank are silently dropped during read."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame(
        {
            "Date": ["2026-03-31", "garbage"],
            "Delegate": ["alpha", "beta"],
            "Total Delegation": ["100", "abc"],
            "Rank": ["1", "xyz"],
        }
    )
    df_new = _make_ranking_df()

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    written = set_mock.call_args.args[1]
    # The parseable old row (2026-03-31, alpha) is preserved; the bad row dropped.
    assert "2026-03-31" in set(written["Date"])
    assert "garbage" not in set(written["Date"])


def test_write_daily_data_handles_unrecognised_existing_header(empty_existing_ws, sheet_io):
    """If the existing header doesn't match the canonical columns, treat as empty."""
    workbook, _ = empty_existing_ws
    # Wrong header order: Daily Data parser checks for an exact prefix match.
    existing = pd.DataFrame(
        {
            "Foo": ["x"],
            "Bar": ["y"],
            "Baz": ["z"],
            "Qux": ["w"],
        }
    )
    df_new = _make_ranking_df()

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    # Only the new rows are written; the misshapen existing is treated as empty.
    written = set_mock.call_args.args[1]
    assert len(written) == 4


def test_write_daily_data_sort_order_date_then_rank(empty_existing_ws, sheet_io):
    workbook, _ = empty_existing_ws
    df = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta", "alpha", "beta"],
            "Date": [date(2026, 4, 2), date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2)],
            "Total Delegation": [90.0, 50.0, 100.0, 60.0],
            "Rank": [1, 2, 1, 2],
        }
    )

    _, set_mock = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    written = set_mock.call_args.args[1]
    # Sorted by Date asc, then Rank asc → first row should be (2026-04-01, rank 1).
    assert written.iloc[0]["Date"] == "2026-04-01"
    assert written.iloc[0]["Rank"] == 1
    assert written.iloc[-1]["Date"] == "2026-04-02"
    assert written.iloc[-1]["Rank"] == 2


def test_write_daily_data_handles_pandas_timestamps(empty_existing_ws, sheet_io):
    """Dates incoming as pandas Timestamp objects are coerced to date(YYYY-MM-DD)."""
    workbook, _ = empty_existing_ws
    df = pd.DataFrame(
        {
            "Delegate": ["alpha", "beta"],
            "Date": [pd.Timestamp("2026-04-01"), pd.Timestamp("2026-04-01")],
            "Total Delegation": [100.0, 50.0],
            "Rank": [1, 2],
        }
    )

    _, set_mock = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    written = set_mock.call_args.args[1]
    assert set(written["Date"]) == {"2026-04-01"}


def test_write_daily_data_extra_columns_ignored(empty_existing_ws, sheet_io):
    """Extra columns in df_ranking don't appear in the written values."""
    workbook, _ = empty_existing_ws
    df = _make_ranking_df()
    df["Extra"] = "junk"

    _, set_mock = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)

    written = set_mock.call_args.args[1]
    assert "Extra" not in written.columns


def test_write_daily_data_empty_dataframe_with_empty_existing(empty_existing_ws, sheet_io):
    workbook, _ = empty_existing_ws
    df_empty = pd.DataFrame(columns=list(sheets.DAILY_DATA_COLUMNS))

    _, set_mock = sheet_io
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_empty)

    written = set_mock.call_args.args[1]
    assert list(written.columns) == list(sheets.DAILY_DATA_COLUMNS)
    assert len(written) == 0


# ---------------------------------------------------------------------------
# write_daily_data — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_daily_data_to_real_workbook(temp_tab, monkeypatch):
    monkeypatch.setattr(sheets, "DAILY_DATA_TAB_TITLE", temp_tab)
    workbook = sheets.get_workbook()
    df = _make_ranking_df()
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df)
    ws = workbook.worksheet(temp_tab)
    assert ws.acell("A1").value == "Date"
    assert ws.acell("D1").value == "Rank"
    assert ws.acell("A2").value == "2026-04-01"


# ---------------------------------------------------------------------------
# Helpers for participation/communication tests
# ---------------------------------------------------------------------------


def _make_participation_df():
    """3-delegate, 2-poll, 1-spell participation df."""
    return pd.DataFrame(
        {
            "Delegate Name": ["BLUE", "Cloaky", "BONAPUBLICA"],
            "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
            "Start Date": ["2025-12-01", "2025-12-01", "2025-12-01"],
            "12345": ["Yes", "No", "Yes"],
            "67890": ["Pending verification", "Yes", "Yes"],
            "0xspell001": ["Yes", "No Delegated SKY", "Yes"],
        }
    )


def _make_poll_info():
    return [
        {
            "pollId": 12345,
            "startDate": date(2026, 4, 5),
            "endDate": date(2026, 4, 7),
            "title": "Approve SubDAO X",
        },
        {
            "pollId": 67890,
            "startDate": date(2026, 4, 12),
            "endDate": date(2026, 4, 14),
            "title": "Adjust risk parameter",
        },
    ]


def _make_spell_info():
    return [
        {
            "address": "0xspell001",
            "startDate": date(2026, 4, 20),
            "endDate": date(2026, 4, 22),
            "title": "Spell: April risk adjustment",
        }
    ]


# ---------------------------------------------------------------------------
# build_participation_dataframe — pure transform
# ---------------------------------------------------------------------------


def test_build_participation_dataframe_returns_header_plus_one_row_per_poll():
    out = sheets.build_participation_dataframe(
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    assert len(out) == 3


def test_build_participation_dataframe_header_format():
    out = sheets.build_participation_dataframe(
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    assert list(out.columns) == [
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "BLUE",
        "Cloaky",
        "BONAPUBLICA",
    ]


def test_build_participation_dataframe_row_format():
    out = sheets.build_participation_dataframe(
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    row = out[out["Poll Id"] == "12345"].iloc[0]
    assert row["Start Date"] == "2026-04-05"
    assert row["End Date"] == "2026-04-07"
    assert row["Title"] == "Approve SubDAO X"
    assert row["BLUE"] == "Yes"
    assert row["Cloaky"] == "No"
    assert row["BONAPUBLICA"] == "Yes"


def test_build_participation_dataframe_spell_has_empty_end_date():
    """Spells don't carry endDate in spell_info; End Date column is blank."""
    spell = [{"address": "0xspell001", "startDate": date(2026, 4, 20), "title": "Spell X"}]
    out = sheets.build_participation_dataframe(_make_participation_df(), _make_poll_info(), spell)
    row = out[out["Poll Id"] == "0xspell001"].iloc[0]
    assert row["Start Date"] == "2026-04-20"
    assert row["End Date"] == ""


def test_build_participation_dataframe_unknown_column_has_blank_metadata():
    """Poll/spell columns missing from metadata get blank cells; status still written."""
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE"],
            "Delegate Contract": ["0xaaa"],
            "Start Date": ["2025-12-01"],
            "99999": ["Yes"],
        }
    )
    out = sheets.build_participation_dataframe(df, _make_poll_info(), _make_spell_info())
    row = out.iloc[0]
    assert row["Poll Id"] == "99999"
    assert row["Start Date"] == ""
    assert row["End Date"] == ""
    assert row["Title"] == ""
    assert row["BLUE"] == "Yes"


def test_build_participation_dataframe_zero_polls_returns_header_only():
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE", "Cloaky"],
            "Delegate Contract": ["0xaaa", "0xbbb"],
            "Start Date": ["2025-12-01", "2025-12-01"],
        }
    )
    out = sheets.build_participation_dataframe(df, [], [])
    assert list(out.columns) == ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"]
    assert len(out) == 0


def test_participation_metadata_columns_pinned():
    assert sheets.PARTICIPATION_METADATA_COLUMNS == (
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
    )


# ---------------------------------------------------------------------------
# Internal helpers — _metadata_by_id, _coerce_date
# ---------------------------------------------------------------------------


def test_metadata_by_id_finds_poll():
    lookup = sheets._metadata_by_id(_make_poll_info(), [])
    assert lookup["12345"]["title"] == "Approve SubDAO X"


def test_metadata_by_id_finds_spell():
    lookup = sheets._metadata_by_id([], _make_spell_info())
    assert lookup["0xspell001"]["title"] == "Spell: April risk adjustment"


def test_metadata_by_id_missing_key_absent():
    assert "nope" not in sheets._metadata_by_id(_make_poll_info(), _make_spell_info())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (date(2026, 4, 1), "2026-04-01"),
        (datetime(2026, 4, 1, 13, 30, tzinfo=UTC), "2026-04-01"),
        (pd.Timestamp("2026-04-01"), "2026-04-01"),
    ],
)
def test_coerce_date(value, expected):
    assert sheets._coerce_date(value) == expected


# ---------------------------------------------------------------------------
# write_participation_raw_data — workbook write
# ---------------------------------------------------------------------------


def test_write_participation_creates_tab_with_correct_title():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_participation_raw_data(
            workbook,
            period,
            _make_participation_df(),
            _make_poll_info(),
            _make_spell_info(),
        )

    assert workbook.add_worksheet.call_args.kwargs["title"] == "Participation Raw Data April 2026"


def test_write_participation_clears_existing_tab_before_writing(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    call_order: list[str] = []
    fake_ws.clear.side_effect = lambda: call_order.append("clear")

    with patch(
        "ad_voting_metrics.sheets.set_with_dataframe",
        side_effect=lambda *_a, **_kw: call_order.append("set"),
    ):
        sheets.write_participation_raw_data(
            workbook,
            MonthPeriod(year=2026, month=4),
            _make_participation_df(),
            _make_poll_info(),
            _make_spell_info(),
        )

    assert call_order == ["clear", "set"]


def test_write_participation_data_rows_one_per_poll_with_metadata(empty_existing_ws):
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_participation_raw_data(
            workbook,
            MonthPeriod(year=2026, month=4),
            _make_participation_df(),
            _make_poll_info(),
            _make_spell_info(),
        )
    written = set_mock.call_args.args[1]
    row_12345 = written[written["Poll Id"] == "12345"].iloc[0]
    assert row_12345["Start Date"] == "2026-04-05"
    assert row_12345["BLUE"] == "Yes"
    assert row_12345["Cloaky"] == "No"

    spell_row = written[written["Poll Id"] == "0xspell001"].iloc[0]
    assert spell_row["Title"] == "Spell: April risk adjustment"
    assert spell_row["End Date"] == "2026-04-22"


# ---------------------------------------------------------------------------
# write_participation_raw_data — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_participation_raw_data_to_real_workbook(temp_tab, monkeypatch):
    monkeypatch.setattr(sheets, "_participation_raw_data_tab_title", lambda _p: temp_tab)
    workbook = sheets.get_workbook()
    sheets.write_participation_raw_data(
        workbook,
        MonthPeriod(year=2026, month=4),
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    ws = workbook.worksheet(temp_tab)
    assert ws.acell("A1").value == "Poll Id"
    assert ws.acell("E1").value == "BLUE"
    assert ws.acell("A2").value == "12345"
    assert ws.acell("B2").value == "2026-04-05"
    assert ws.acell("E2").value == "Yes"


# ---------------------------------------------------------------------------
# write_communication_master — workbook write
# ---------------------------------------------------------------------------


def test_write_communication_master_missing_df_column_raises():
    workbook = MagicMock()
    df_bad = pd.DataFrame({"NotTheRightColumn": ["foo"]})
    with pytest.raises(ValueError, match="Delegate Name"):
        sheets.write_communication_master(
            workbook,
            df_bad,
            _make_poll_info(),
            _make_spell_info(),
        )


def test_write_communication_master_first_fetch_creates_header(empty_existing_ws, sheet_io):
    """First fetch (empty tab): header is metadata columns + delegate names in df order."""
    workbook, _ = empty_existing_ws
    _, set_mock = sheet_io
    sheets.write_communication_master(
        workbook,
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    written = set_mock.call_args.args[1]
    assert list(written.columns) == [
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "BLUE",
        "Cloaky",
        "BONAPUBLICA",
    ]


@pytest.mark.parametrize(
    ("participation", "expected_default"),
    [
        ("Yes", "Pending verification"),  # participated → awaits operator verification
        ("No", "Did not vote"),  # did not participate
        ("No Delegated SKY", "No Delegated SKY"),  # discounted status mirrors through
    ],
)
def test_write_communication_master_first_fetch_defaults(participation, expected_default, empty_existing_ws, sheet_io):
    """On first fetch, each participation status maps to its communication default via cross_reference_one."""
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE"],
            "Delegate Contract": ["0xaaa"],
            "Start Date": ["2025-12-01"],
            "12345": [participation],
        }
    )
    workbook, _ = empty_existing_ws
    _, set_mock = sheet_io
    sheets.write_communication_master(workbook, df, _make_poll_info(), [])
    written = set_mock.call_args.args[1]
    assert written.iloc[0]["BLUE"] == expected_default


def test_write_communication_master_missing_column_raises(empty_existing_ws):
    """New delegate added to YAML but not to the existing tab → raise."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["55555"],
            "Start Date": ["2026-03-01"],
            "End Date": ["2026-03-03"],
            "Title": ["Old poll"],
            "BLUE": ["Yes"],
        }
    )
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE", "NewDelegate"],
            "Delegate Contract": ["0xaaa", "0xddd"],
            "Start Date": ["2025-12-01", "2025-12-01"],
            "12345": ["Yes", "Yes"],
        }
    )
    workbook, _ = empty_existing_ws
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=existing),
        patch("ad_voting_metrics.sheets.set_with_dataframe"),
        pytest.raises(ValueError, match="missing column"),
    ):
        sheets.write_communication_master(workbook, df, _make_poll_info(), [])


def test_write_communication_master_preserves_operator_edits(empty_existing_ws, sheet_io):
    """Non-blank existing cells survive the rewrite."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["12345"],
            "Start Date": ["2026-04-05"],
            "End Date": ["2026-04-07"],
            "Title": ["Approve SubDAO X"],
            "BLUE": ["Operator-set value"],  # non-blank → must be preserved
            "Cloaky": [""],  # blank → filled with default
            "BONAPUBLICA": [""],
        }
    )
    df = _make_participation_df()
    workbook, _ = empty_existing_ws
    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())
    written = set_mock.call_args.args[1]
    row = written[written["Poll Id"] == "12345"].iloc[0]
    assert row["BLUE"] == "Operator-set value"
    assert row["Cloaky"] == "Did not vote"


def test_write_communication_master_backs_up_existing_before_clear(empty_existing_ws, sheet_io, backup_dir):
    """Non-empty existing data (with operator edits) is saved to a local CSV before the tab is cleared."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["12345"],
            "Start Date": ["2026-04-05"],
            "End Date": ["2026-04-07"],
            "Title": ["Approve SubDAO X"],
            "BLUE": ["Operator-set value"],
            "Cloaky": [""],
            "BONAPUBLICA": [""],
        }
    )
    df = _make_participation_df()
    workbook, _ = empty_existing_ws
    get_mock, _ = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())

    backups = list(backup_dir.glob("Communication Master_*.csv"))
    assert len(backups) == 1
    backup = pd.read_csv(backups[0])
    assert list(backup["Poll Id"].astype(str)) == ["12345"]
    assert list(backup["BLUE"]) == ["Operator-set value"]


def test_write_communication_master_preserves_historical_polls(empty_existing_ws, sheet_io):
    """A poll in the existing tab but not in the current fetch stays in the output."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["55555"],
            "Start Date": ["2026-03-01"],
            "End Date": ["2026-03-03"],
            "Title": ["Old poll"],
            "BLUE": ["Yes"],
            "Cloaky": ["No"],
            "BONAPUBLICA": [""],
        }
    )
    df = _make_participation_df()  # contains polls 12345, 67890, 0xspell001
    workbook, _ = empty_existing_ws
    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())
    written = set_mock.call_args.args[1]
    assert "55555" in set(written["Poll Id"])
    assert "12345" in set(written["Poll Id"])


def test_write_communication_master_historical_delegate_cells_blank_for_new_polls(empty_existing_ws, sheet_io):
    """An out-of-roster delegate column stays blank for newly added polls."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["55555"],
            "Start Date": ["2026-03-01"],
            "End Date": [""],
            "Title": ["Old poll"],
            "BLUE": ["Yes"],
            "Cloaky": ["Yes"],
            "BONAPUBLICA": ["Yes"],
            "GoneDelegate": ["Yes"],  # historical column, delegate no longer in roster
        }
    )
    df = _make_participation_df()
    workbook, _ = empty_existing_ws
    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())
    written = set_mock.call_args.args[1]
    new_row = written[written["Poll Id"] == "12345"].iloc[0]
    # GoneDelegate column stays blank for a poll that isn't in their history.
    assert new_row["GoneDelegate"] == ""


def test_write_communication_master_sort_order_start_date_descending(empty_existing_ws, sheet_io):
    """Output is sorted newest first; unparseable Start Date rows go to the end."""
    existing = pd.DataFrame(
        {
            "Poll Id": ["alpha", "beta"],
            "Start Date": ["2026-01-01", "not-a-date"],
            "End Date": ["", ""],
            "Title": ["A", "B"],
            "BLUE": ["Yes", "Yes"],
            "Cloaky": ["Yes", "Yes"],
            "BONAPUBLICA": ["Yes", "Yes"],
        }
    )
    df = _make_participation_df()
    workbook, _ = empty_existing_ws
    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())
    written = set_mock.call_args.args[1]
    # The first row should be the latest valid Start Date (0xspell001 → 2026-04-20).
    assert written.iloc[0]["Poll Id"] == "0xspell001"
    # The last row should be "beta" (unparseable Start Date sorts last).
    assert written.iloc[-1]["Poll Id"] == "beta"


def test_write_communication_master_clears_before_writing(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    call_order: list[str] = []
    fake_ws.clear.side_effect = lambda: call_order.append("clear")

    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=pd.DataFrame()),
        patch(
            "ad_voting_metrics.sheets.set_with_dataframe",
            side_effect=lambda *_a, **_kw: call_order.append("set"),
        ),
    ):
        sheets.write_communication_master(
            workbook,
            _make_participation_df(),
            _make_poll_info(),
            _make_spell_info(),
        )
    assert call_order == ["clear", "set"]


def test_write_communication_master_uses_correct_tab_name(sheet_io):
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    _, _ = sheet_io
    sheets.write_communication_master(
        workbook,
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    assert workbook.add_worksheet.call_args.kwargs["title"] == "Communication Master"


# ---------------------------------------------------------------------------
# write_communication_master — integration
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_communication_master_to_real_workbook(temp_tab, monkeypatch):
    monkeypatch.setattr(sheets, "COMMUNICATION_MASTER_TAB_TITLE", temp_tab)
    workbook = sheets.get_workbook()
    sheets.write_communication_master(
        workbook,
        _make_participation_df(),
        _make_poll_info(),
        _make_spell_info(),
    )
    ws = workbook.worksheet(temp_tab)
    assert ws.acell("A1").value == "Poll Id"


# ---------------------------------------------------------------------------
# guard: NaN handling at the I/O boundary
# ---------------------------------------------------------------------------


def test_read_sheet_as_strings_fills_nan_with_blank(empty_existing_ws):
    """get_as_dataframe may return NaN for some empty cells; we coerce to ''."""
    workbook, _ = empty_existing_ws
    df_with_nan = pd.DataFrame({"A": ["x", np.nan], "B": [np.nan, "y"]})
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df_with_nan):
        out = sheets._read_sheet_as_strings(workbook.worksheet())
    assert out.iloc[0]["B"] == ""
    assert out.iloc[1]["A"] == ""
