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
from ad_voting_metrics.compensation import (
    CompensationConfig,
    DelegateCompensation,
    PeriodCompensation,
)
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


def test_get_workbook_error_message_points_at_env_example(monkeypatch):
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    with pytest.raises(RuntimeError, match=r"\.env\.example"):
        sheets.get_workbook(workbook_id="anything")


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
        json.dumps({
            "type": "service_account",
            "client_email": "fake@example.iam.gserviceaccount.com",
            "private_key": "-----BEGIN PRIVATE KEY-----\n-----END PRIVATE KEY-----\n",
            "token_uri": "https://oauth2.googleapis.com/token",
        }),
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
# list_tab_names + get_or_create_tab + clear_tab
# ---------------------------------------------------------------------------


def test_list_tab_names_returns_titles_in_order():
    workbook = MagicMock()
    workbook.worksheets.return_value = [
        MagicMock(title="Daily Data"),
        MagicMock(title="Config"),
        MagicMock(title="Communication Master"),
    ]
    assert sheets.list_tab_names(workbook) == ["Daily Data", "Config", "Communication Master"]


def test_list_tab_names_empty_workbook_returns_empty_list():
    workbook = MagicMock()
    workbook.worksheets.return_value = []
    assert sheets.list_tab_names(workbook) == []


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
def test_list_tab_names_returns_real_tabs():
    workbook = sheets.get_workbook()
    tabs = sheets.list_tab_names(workbook)
    assert isinstance(tabs, list)
    assert all(isinstance(t, str) for t in tabs)


@pytest.mark.integration
def test_get_or_create_tab_creates_and_finds_real_tab(temp_tab):
    workbook = sheets.get_workbook()
    ws = sheets.get_or_create_tab(workbook, temp_tab, rows=10, cols=4)
    assert ws.title == temp_tab
    # Second call returns the same tab
    ws2 = sheets.get_or_create_tab(workbook, temp_tab, rows=10, cols=4)
    assert ws2.id == ws.id


@pytest.mark.integration
def test_clear_tab_wipes_real_cells(temp_tab):
    workbook = sheets.get_workbook()
    ws = workbook.worksheet(temp_tab)
    ws.update(values=[["hello", "world"], ["foo", "bar"]], range_name="A1:B2")
    assert ws.acell("A1").value == "hello"
    sheets.clear_tab(ws)
    assert ws.acell("A1").value in {None, ""}


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


# ---------------------------------------------------------------------------
# write_daily_data — schema enforcement + merge behavior
# ---------------------------------------------------------------------------


def _make_ranking_df():
    """Two-day ranking df with two delegates per day."""
    return pd.DataFrame({
        "Delegate": ["alpha", "beta", "alpha", "beta"],
        "Date": [date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 2)],
        "Total Delegation": [100.0, 50.0, 90.0, 60.0],
        "Rank": [1, 2, 1, 2],
    })


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
    existing = pd.DataFrame({
        "Date": ["2026-03-31", "2026-03-31"],
        "Delegate": ["alpha", "beta"],
        "Total Delegation": ["100", "50"],
        "Rank": ["1", "2"],
    })
    df_new = pd.DataFrame({
        "Delegate": ["alpha", "beta"],
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Total Delegation": [200.0, 150.0],
        "Rank": [1, 2],
    })

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
    existing = pd.DataFrame({
        "Date": ["2026-04-01", "2026-04-01"],
        "Delegate": ["alpha", "beta"],
        "Total Delegation": ["999", "999"],  # stale
        "Rank": ["9", "9"],
    })
    df_new = pd.DataFrame({
        "Delegate": ["alpha", "beta"],
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Total Delegation": [100.0, 50.0],
        "Rank": [1, 2],
    })

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    written = set_mock.call_args.args[1]
    assert len(written) == 2
    assert set(written["Total Delegation"]) == {100.0, 50.0}
    assert 999 not in set(written["Rank"])


def test_write_daily_data_raises_on_roster_drift(empty_existing_ws):
    """A date whose delegate-row count changed between fetches is fatal."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame({
        "Date": ["2026-04-01", "2026-04-01", "2026-04-01"],
        "Delegate": ["alpha", "beta", "gamma"],
        "Total Delegation": ["100", "50", "25"],
        "Rank": ["1", "2", "3"],
    })
    df_new = pd.DataFrame({
        "Delegate": ["alpha", "beta"],
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Total Delegation": [100.0, 50.0],
        "Rank": [1, 2],
    })

    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=existing),
        patch("ad_voting_metrics.sheets.set_with_dataframe"),
        pytest.raises(ValueError, match="Roster drift"),
    ):
        sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)


def test_write_daily_data_no_drift_when_dates_dont_overlap(empty_existing_ws, sheet_io):
    """Per-date row count is only checked for dates appearing in both."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame({
        "Date": ["2026-03-31", "2026-03-31", "2026-03-31"],
        "Delegate": ["alpha", "beta", "gamma"],
        "Total Delegation": ["100", "50", "25"],
        "Rank": ["1", "2", "3"],
    })
    df_new = pd.DataFrame({
        "Delegate": ["alpha", "beta"],
        "Date": [date(2026, 4, 1), date(2026, 4, 1)],
        "Total Delegation": [100.0, 50.0],
        "Rank": [1, 2],
    })

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    # 3 + 2 = 5 rows, no drift error
    assert len(set_mock.call_args.args[1]) == 5


def test_write_daily_data_handles_unparseable_existing_rows(empty_existing_ws, sheet_io):
    """Existing rows with non-numeric Rank are silently dropped during read."""
    workbook, _ = empty_existing_ws
    existing = pd.DataFrame({
        "Date": ["2026-03-31", "garbage"],
        "Delegate": ["alpha", "beta"],
        "Total Delegation": ["100", "abc"],
        "Rank": ["1", "xyz"],
    })
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
    existing = pd.DataFrame({
        "Foo": ["x"],
        "Bar": ["y"],
        "Baz": ["z"],
        "Qux": ["w"],
    })
    df_new = _make_ranking_df()

    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_daily_data(workbook, MonthPeriod(year=2026, month=4), df_new)

    # Only the new rows are written; the misshapen existing is treated as empty.
    written = set_mock.call_args.args[1]
    assert len(written) == 4


def test_write_daily_data_sort_order_date_then_rank(empty_existing_ws, sheet_io):
    workbook, _ = empty_existing_ws
    df = pd.DataFrame({
        "Delegate": ["alpha", "beta", "alpha", "beta"],
        "Date": [date(2026, 4, 2), date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2)],
        "Total Delegation": [90.0, 50.0, 100.0, 60.0],
        "Rank": [1, 2, 1, 2],
    })

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
    df = pd.DataFrame({
        "Delegate": ["alpha", "beta"],
        "Date": [pd.Timestamp("2026-04-01"), pd.Timestamp("2026-04-01")],
        "Total Delegation": [100.0, 50.0],
        "Rank": [1, 2],
    })

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
    return pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky", "BONAPUBLICA"],
        "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
        "Start Date": ["2025-12-01", "2025-12-01", "2025-12-01"],
        "12345": ["Yes", "No", "Yes"],
        "67890": ["Pending verification", "Yes", "Yes"],
        "0xspell001": ["Yes", "No Delegated SKY", "Yes"],
    })


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
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "99999": ["Yes"],
    })
    out = sheets.build_participation_dataframe(df, _make_poll_info(), _make_spell_info())
    row = out.iloc[0]
    assert row["Poll Id"] == "99999"
    assert row["Start Date"] == ""
    assert row["End Date"] == ""
    assert row["Title"] == ""
    assert row["BLUE"] == "Yes"


def test_build_participation_dataframe_zero_polls_returns_header_only():
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
    })
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
# Internal helpers — _lookup_poll_or_spell, _coerce_date
# ---------------------------------------------------------------------------


def test_lookup_poll_or_spell_finds_poll():
    poll_info = _make_poll_info()
    result = sheets._lookup_poll_or_spell("12345", poll_info, [])
    assert result is not None
    assert result["title"] == "Approve SubDAO X"


def test_lookup_poll_or_spell_finds_spell():
    spell_info = _make_spell_info()
    result = sheets._lookup_poll_or_spell("0xspell001", [], spell_info)
    assert result is not None
    assert result["title"] == "Spell: April risk adjustment"


def test_lookup_poll_or_spell_returns_none_when_missing():
    assert sheets._lookup_poll_or_spell("nope", _make_poll_info(), _make_spell_info()) is None


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


def test_write_participation_header_includes_metadata_and_delegate_names(empty_existing_ws):
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
    assert list(written.columns) == [
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "BLUE",
        "Cloaky",
        "BONAPUBLICA",
    ]


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


def test_write_participation_unknown_poll_id_has_blank_metadata(empty_existing_ws):
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "99999": ["Yes"],
    })
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_participation_raw_data(
            workbook,
            MonthPeriod(year=2026, month=4),
            df,
            _make_poll_info(),
            _make_spell_info(),
        )
    written = set_mock.call_args.args[1]
    row = written.iloc[0]
    assert row["Poll Id"] == "99999"
    assert row["Start Date"] == ""
    assert row["BLUE"] == "Yes"


def test_write_participation_zero_polls_writes_header_only(empty_existing_ws):
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "Cloaky"],
        "Delegate Contract": ["0xaaa", "0xbbb"],
        "Start Date": ["2025-12-01", "2025-12-01"],
    })
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_participation_raw_data(
            workbook,
            MonthPeriod(year=2026, month=4),
            df,
            [],
            [],
        )
    written = set_mock.call_args.args[1]
    assert list(written.columns) == ["Poll Id", "Start Date", "End Date", "Title", "BLUE", "Cloaky"]
    assert len(written) == 0


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
    df = pd.DataFrame({
        "Delegate Name": ["BLUE"],
        "Delegate Contract": ["0xaaa"],
        "Start Date": ["2025-12-01"],
        "12345": [participation],
    })
    workbook, _ = empty_existing_ws
    _, set_mock = sheet_io
    sheets.write_communication_master(workbook, df, _make_poll_info(), [])
    written = set_mock.call_args.args[1]
    assert written.iloc[0]["BLUE"] == expected_default


def test_write_communication_master_missing_column_raises(empty_existing_ws):
    """New delegate added to YAML but not to the existing tab → raise."""
    existing = pd.DataFrame({
        "Poll Id": ["55555"],
        "Start Date": ["2026-03-01"],
        "End Date": ["2026-03-03"],
        "Title": ["Old poll"],
        "BLUE": ["Yes"],
    })
    df = pd.DataFrame({
        "Delegate Name": ["BLUE", "NewDelegate"],
        "Delegate Contract": ["0xaaa", "0xddd"],
        "Start Date": ["2025-12-01", "2025-12-01"],
        "12345": ["Yes", "Yes"],
    })
    workbook, _ = empty_existing_ws
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=existing),
        patch("ad_voting_metrics.sheets.set_with_dataframe"),
        pytest.raises(ValueError, match="missing column"),
    ):
        sheets.write_communication_master(workbook, df, _make_poll_info(), [])


def test_write_communication_master_preserves_operator_edits(empty_existing_ws, sheet_io):
    """Non-blank existing cells survive the rewrite."""
    existing = pd.DataFrame({
        "Poll Id": ["12345"],
        "Start Date": ["2026-04-05"],
        "End Date": ["2026-04-07"],
        "Title": ["Approve SubDAO X"],
        "BLUE": ["Operator-set value"],  # non-blank → must be preserved
        "Cloaky": [""],  # blank → filled with default
        "BONAPUBLICA": [""],
    })
    df = _make_participation_df()
    workbook, _ = empty_existing_ws
    get_mock, set_mock = sheet_io
    get_mock.return_value = existing
    sheets.write_communication_master(workbook, df, _make_poll_info(), _make_spell_info())
    written = set_mock.call_args.args[1]
    row = written[written["Poll Id"] == "12345"].iloc[0]
    assert row["BLUE"] == "Operator-set value"
    assert row["Cloaky"] == "Did not vote"


def test_write_communication_master_preserves_historical_polls(empty_existing_ws, sheet_io):
    """A poll in the existing tab but not in the current fetch stays in the output."""
    existing = pd.DataFrame({
        "Poll Id": ["55555"],
        "Start Date": ["2026-03-01"],
        "End Date": ["2026-03-03"],
        "Title": ["Old poll"],
        "BLUE": ["Yes"],
        "Cloaky": ["No"],
        "BONAPUBLICA": [""],
    })
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
    existing = pd.DataFrame({
        "Poll Id": ["55555"],
        "Start Date": ["2026-03-01"],
        "End Date": [""],
        "Title": ["Old poll"],
        "BLUE": ["Yes"],
        "Cloaky": ["Yes"],
        "BONAPUBLICA": ["Yes"],
        "GoneDelegate": ["Yes"],  # historical column, delegate no longer in roster
    })
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
    existing = pd.DataFrame({
        "Poll Id": ["alpha", "beta"],
        "Start Date": ["2026-01-01", "not-a-date"],
        "End Date": ["", ""],
        "Title": ["A", "B"],
        "BLUE": ["Yes", "Yes"],
        "Cloaky": ["Yes", "Yes"],
        "BONAPUBLICA": ["Yes", "Yes"],
    })
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
# read_config
# ---------------------------------------------------------------------------


def _make_config_df(rows: list[list[str]]) -> pd.DataFrame:
    """Wrap a list-of-rows in a DataFrame with first row as header."""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows[1:], columns=rows[0])


def test_read_config_missing_tab_raises_runtime_error():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    with pytest.raises(RuntimeError, match="missing the 'Config' tab"):
        sheets.read_config(workbook)


def test_read_config_empty_tab_raises_value_error(empty_existing_ws):
    workbook, _ = empty_existing_ws
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=pd.DataFrame()),
        pytest.raises(ValueError, match="is empty"),
    ):
        sheets.read_config(workbook)


def test_read_config_missing_required_key_raises(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_config_df([
        ["Key", "Value"],
        ["L1_USDS", "1000"],
        ["L2_USDS", "500"],
        # L3_USDS and TOTAL_SLOTS missing
    ])
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df),
        pytest.raises(ValueError, match="missing required keys"),
    ):
        sheets.read_config(workbook)


def test_read_config_unparseable_value_raises(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_config_df([
        ["Key", "Value"],
        ["L1_USDS", "not a number"],
        ["L2_USDS", "500"],
        ["L3_USDS", "100"],
        ["TOTAL_SLOTS", "6"],
    ])
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df),
        pytest.raises(ValueError, match="un-parseable"),
    ):
        sheets.read_config(workbook)


def test_read_config_happy_path_returns_compensation_config(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_config_df([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        config = sheets.read_config(workbook)
    assert config.l1_usds == 33333.0
    assert config.l2_usds == 14583.0
    assert config.l3_usds == 4000.0
    assert config.total_slots == 6


def test_read_config_unknown_keys_silently_ignored(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_config_df([
        ["Key", "Value"],
        ["L1_USDS", "33333"],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
        ["UNKNOWN_KEY", "whatever"],
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        config = sheets.read_config(workbook)
    assert config.total_slots == 6


def test_read_config_strips_whitespace_from_keys_and_values(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_config_df([
        ["Key", "Value"],
        ["  L1_USDS  ", "  33333  "],
        ["L2_USDS", "14583"],
        ["L3_USDS", "4000"],
        ["TOTAL_SLOTS", "6"],
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        config = sheets.read_config(workbook)
    assert config.l1_usds == 33333.0


# ---------------------------------------------------------------------------
# write_compensation_tab
# ---------------------------------------------------------------------------


def _make_period_comp(
    *,
    period: MonthPeriod | None = None,
    per_delegate: list | None = None,
    days_in_period: int = 30,
    slot_days_check: str = "GOOD",
):
    """Build a minimal PeriodCompensation for write_compensation_tab tests."""
    if period is None:
        period = MonthPeriod(year=2026, month=4)
    if per_delegate is None:
        per_delegate = [
            DelegateCompensation(
                name="alpha",
                rank_at_period_end=1,
                level_at_period_end=1,
                days_as_l1=30,
                days_as_l2=0,
                days_as_l3=0,
                participation_pct=0.95,
                communication_pct=0.90,
                metrics_modifier=1.0,
                entitlement_pre_modifier=33333.0,
                final_amount=33333.0,
                buffer_carry_in=0.0,
                buffer_added=33333.0,
                payment_amount=0.0,
                buffer_post_payment=0.0,
                notes="",
            ),
        ]
    config = CompensationConfig(l1_usds=33333.0, l2_usds=14583.0, l3_usds=4000.0, total_slots=6)
    return PeriodCompensation(
        period=period,
        config=config,
        days_in_period=days_in_period,
        per_delegate=per_delegate,
        validation={"slot_days_check": slot_days_check},
    )


def test_write_compensation_tab_uses_correct_tab_name():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp())
    assert workbook.add_worksheet.call_args.kwargs["title"] == "April 2026 Compensation"


def test_write_compensation_tab_writes_header_block(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp())
    header_block = fake_ws.update.call_args.kwargs["values"]
    assert header_block[0][0] == "Year"
    assert header_block[0][1] == 2026
    assert header_block[1][0] == "Month"
    assert header_block[1][1] == "April"
    assert header_block[2][0] == "Period Start"
    assert header_block[3][0] == "Period End"
    assert header_block[4][0] == "Days in Month"


def test_write_compensation_tab_writes_config_reference_amounts(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp())
    header_block = fake_ws.update.call_args.kwargs["values"]
    assert header_block[0][3] == "Level 1 USDS"
    assert header_block[0][4] == 33333.0
    assert header_block[1][3] == "Level 2 USDS"
    assert header_block[1][4] == 14583.0
    assert header_block[2][3] == "Level 3 USDS"
    assert header_block[2][4] == 4000.0


def test_write_compensation_tab_writes_level_counts(empty_existing_ws):
    """Number of Level X is the count of delegates with each end-of-period level."""
    delegates = [
        DelegateCompensation(
            name=f"d{i}",
            rank_at_period_end=i,
            level_at_period_end=lvl,
            days_as_l1=0,
            days_as_l2=0,
            days_as_l3=0,
            participation_pct=None,
            communication_pct=None,
            metrics_modifier=0.0,
            entitlement_pre_modifier=0.0,
            final_amount=0.0,
            buffer_carry_in=0.0,
            buffer_added=0.0,
            payment_amount=0.0,
            buffer_post_payment=0.0,
            notes="",
        )
        for i, lvl in enumerate([1, 1, 2, 3, 3, 3], start=1)
    ]
    workbook, fake_ws = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=delegates))
    header_block = fake_ws.update.call_args.kwargs["values"]
    assert header_block[0][7] == 2  # n_l1
    assert header_block[1][7] == 1  # n_l2
    assert header_block[2][7] == 3  # n_l3


def test_write_compensation_tab_total_is_computed_in_python(empty_existing_ws):
    """Total Final Amount is the sum of per-delegate finals (no =SUM formula)."""
    delegates = [
        DelegateCompensation(
            name=f"d{i}",
            rank_at_period_end=i,
            level_at_period_end=1,
            days_as_l1=30,
            days_as_l2=0,
            days_as_l3=0,
            participation_pct=0.95,
            communication_pct=0.95,
            metrics_modifier=1.0,
            entitlement_pre_modifier=1000.0,
            final_amount=1000.0,
            buffer_carry_in=0.0,
            buffer_added=1000.0,
            payment_amount=0.0,
            buffer_post_payment=0.0,
            notes="",
        )
        for i in range(3)
    ]
    workbook, fake_ws = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=delegates))
    header_block = fake_ws.update.call_args.kwargs["values"]
    assert header_block[6][0] == "Total Final Amount"
    assert header_block[6][1] == 3000.0
    # Crucially: it's a number, not a formula string.
    assert not (isinstance(header_block[6][1], str) and header_block[6][1].startswith("="))


def test_write_compensation_tab_writes_slot_days_check(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe"):
        sheets.write_compensation_tab(workbook, _make_period_comp(slot_days_check="NOT GOOD"))
    header_block = fake_ws.update.call_args.kwargs["values"]
    assert header_block[7][0] == "Slot Days Check"
    assert header_block[7][1] == "NOT GOOD"


def test_write_compensation_tab_data_table_starts_at_row_9(empty_existing_ws):
    """The data table (column headers + data) is written at row 9."""
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp())
    assert set_mock.call_args.kwargs["row"] == 9


def test_write_compensation_tab_data_columns_match_canonical_header(empty_existing_ws):
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp())
    written = set_mock.call_args.args[1]
    assert list(written.columns) == list(sheets.COMPENSATION_COLUMNS)


def test_write_compensation_tab_data_rows_in_order(empty_existing_ws):
    """Rows appear in the order of period_comp.per_delegate (alphabetical from compute)."""
    delegates = [
        DelegateCompensation(
            name=name,
            rank_at_period_end=None,
            level_at_period_end=None,
            days_as_l1=0,
            days_as_l2=0,
            days_as_l3=0,
            participation_pct=None,
            communication_pct=None,
            metrics_modifier=0.0,
            entitlement_pre_modifier=0.0,
            final_amount=0.0,
            buffer_carry_in=0.0,
            buffer_added=0.0,
            payment_amount=0.0,
            buffer_post_payment=0.0,
            notes="",
        )
        for name in ["alpha", "beta", "gamma"]
    ]
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=delegates))
    written = set_mock.call_args.args[1]
    assert list(written["Delegate"]) == ["alpha", "beta", "gamma"]


def test_write_compensation_tab_none_pct_renders_as_no_data(empty_existing_ws):
    """None for participation_pct / communication_pct → 'No Data' string in the cell."""
    delegate = DelegateCompensation(
        name="alpha",
        rank_at_period_end=1,
        level_at_period_end=1,
        days_as_l1=30,
        days_as_l2=0,
        days_as_l3=0,
        participation_pct=None,
        communication_pct=None,
        metrics_modifier=0.0,
        entitlement_pre_modifier=0.0,
        final_amount=0.0,
        buffer_carry_in=0.0,
        buffer_added=0.0,
        payment_amount=0.0,
        buffer_post_payment=0.0,
        notes="",
    )
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=[delegate]))
    written = set_mock.call_args.args[1]
    assert written.iloc[0]["Participation 6-month %"] == "No Data"
    assert written.iloc[0]["Communication 6-month %"] == "No Data"


def test_write_compensation_tab_level_label_mapping(empty_existing_ws):
    """level_at_period_end (1/2/3/None) → Level 1 / Level 2 / Level 3 / No."""

    def _delegate(level):
        return DelegateCompensation(
            name=f"d{level}",
            rank_at_period_end=1,
            level_at_period_end=level,
            days_as_l1=0,
            days_as_l2=0,
            days_as_l3=0,
            participation_pct=None,
            communication_pct=None,
            metrics_modifier=0.0,
            entitlement_pre_modifier=0.0,
            final_amount=0.0,
            buffer_carry_in=0.0,
            buffer_added=0.0,
            payment_amount=0.0,
            buffer_post_payment=0.0,
            notes="",
        )

    delegates = [_delegate(1), _delegate(2), _delegate(3), _delegate(None)]
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=delegates))
    written = set_mock.call_args.args[1]
    assert list(written["Ranked During Month?"]) == ["Level 1", "Level 2", "Level 3", "No"]


def test_write_compensation_tab_empty_per_delegate_still_writes_header(empty_existing_ws):
    """Empty roster: data table is empty but column headers still appear."""
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.set_with_dataframe") as set_mock:
        sheets.write_compensation_tab(workbook, _make_period_comp(per_delegate=[]))
    written = set_mock.call_args.args[1]
    assert len(written) == 0
    assert list(written.columns) == list(sheets.COMPENSATION_COLUMNS)


def test_write_compensation_tab_clears_before_writing(empty_existing_ws):
    workbook, fake_ws = empty_existing_ws
    call_order: list[str] = []
    fake_ws.clear.side_effect = lambda: call_order.append("clear")
    fake_ws.update.side_effect = lambda **_kw: call_order.append("update")

    with patch(
        "ad_voting_metrics.sheets.set_with_dataframe",
        side_effect=lambda *_a, **_kw: call_order.append("set"),
    ):
        sheets.write_compensation_tab(workbook, _make_period_comp())

    assert call_order[0] == "clear"
    assert "update" in call_order  # header block write
    assert "set" in call_order  # data table write
    assert call_order.index("clear") < call_order.index("update")
    assert call_order.index("update") < call_order.index("set")


# ---------------------------------------------------------------------------
# read_daily_data
# ---------------------------------------------------------------------------


def _make_daily_data_df(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    """Build a DataFrame in the Daily Data sheet shape (all string cells)."""
    return pd.DataFrame(rows, columns=list(sheets.DAILY_DATA_COLUMNS))


def test_read_daily_data_missing_tab_raises_with_operator_message():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    with pytest.raises(RuntimeError, match="Run `fetch`"):
        sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))


def test_read_daily_data_no_rows_in_period_raises(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_daily_data_df([
        ("2026-03-15", "alpha", "100", "1"),  # outside April
    ])
    with (
        patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df),
        pytest.raises(RuntimeError, match="has no rows for April 2026"),
    ):
        sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))


def test_read_daily_data_returns_ranks_for_every_day_present(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_daily_data_df([
        ("2026-04-01", "alpha", "100", "1"),
        ("2026-04-01", "beta", "50", "2"),
        ("2026-04-02", "alpha", "90", "1"),
        ("2026-04-02", "beta", "60", "2"),
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))
    assert len(out) == 4
    assert set(out["Date"]) == {date(2026, 4, 1), date(2026, 4, 2)}
    assert set(out["Rank"]) == {1, 2}


def test_read_daily_data_filters_out_other_months(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_daily_data_df([
        ("2026-03-31", "alpha", "100", "1"),  # outside
        ("2026-04-01", "alpha", "100", "1"),
        ("2026-05-01", "alpha", "90", "1"),  # outside
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))
    assert list(out["Date"]) == [date(2026, 4, 1)]


def test_read_daily_data_skips_unparseable_dates(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = _make_daily_data_df([
        ("2026-04-01", "alpha", "100", "1"),
        ("garbage", "beta", "50", "2"),
    ])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_daily_data(workbook, MonthPeriod(year=2026, month=4))
    assert list(out["Delegate"]) == ["alpha"]


# ---------------------------------------------------------------------------
# read_participation_for_window
# ---------------------------------------------------------------------------


def _participation_tab_df(rows: list[tuple[str, ...]], delegates: list[str]) -> pd.DataFrame:
    """Build a Participation Raw Data sheet-shaped DataFrame (one row per poll)."""
    cols = ["Poll Id", "Start Date", "End Date", "Title", *delegates]
    return pd.DataFrame(rows, columns=cols)


def test_read_participation_for_window_missing_tabs_skipped_silently():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    out = sheets.read_participation_for_window(workbook, date(2026, 1, 1), date(2026, 4, 30))
    assert isinstance(out, pd.DataFrame)
    assert out.empty
    assert list(out.columns) == [
        "Delegate",
        "Poll Id",
        "Start Date",
        "End Date",
        "Title",
        "Participation Status",
    ]


def test_read_participation_for_window_aggregates_across_months():
    workbook = MagicMock()
    fake_apr_ws = MagicMock(spec=gspread.Worksheet)
    fake_mar_ws = MagicMock(spec=gspread.Worksheet)

    def _ws_lookup(title):
        if title == "Participation Raw Data April 2026":
            return fake_apr_ws
        if title == "Participation Raw Data March 2026":
            return fake_mar_ws
        raise gspread.exceptions.WorksheetNotFound

    workbook.worksheet.side_effect = _ws_lookup

    apr_df = _participation_tab_df(
        [("12345", "2026-04-05", "2026-04-07", "April poll", "Yes", "No")],
        ["alpha", "beta"],
    )
    mar_df = _participation_tab_df(
        [("11111", "2026-03-10", "2026-03-12", "March poll", "No", "Yes")],
        ["alpha", "beta"],
    )

    def _read(ws, **_kwargs):
        if ws is fake_apr_ws:
            return apr_df
        if ws is fake_mar_ws:
            return mar_df
        return pd.DataFrame()

    with patch("ad_voting_metrics.sheets.get_as_dataframe", side_effect=_read):
        out = sheets.read_participation_for_window(workbook, date(2026, 3, 1), date(2026, 4, 30))

    assert len(out) == 4  # 2 polls x 2 delegates
    alpha_apr = out[(out["Delegate"] == "alpha") & (out["Poll Id"] == "12345")].iloc[0]
    assert alpha_apr["Participation Status"] == "Yes"
    assert alpha_apr["Start Date"] == date(2026, 4, 5)
    beta_mar = out[(out["Delegate"] == "beta") & (out["Poll Id"] == "11111")].iloc[0]
    assert beta_mar["Participation Status"] == "Yes"


def test_read_participation_for_window_filters_polls_outside_window():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    apr_df = _participation_tab_df(
        [
            ("12345", "2026-04-05", "", "in window", "Yes"),
            ("99999", "2025-12-15", "", "before window", "Yes"),
        ],
        ["alpha"],
    )
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=apr_df):
        out = sheets.read_participation_for_window(workbook, date(2026, 1, 1), date(2026, 4, 30))
    poll_ids = set(out["Poll Id"])
    assert "12345" in poll_ids
    assert "99999" not in poll_ids


def test_read_participation_for_window_skips_rows_with_unparseable_dates():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    apr_df = _participation_tab_df(
        [
            ("12345", "2026-04-05", "", "ok", "Yes"),
            ("BAD", "not-a-date", "", "skip me", "Yes"),
        ],
        ["alpha"],
    )
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=apr_df):
        out = sheets.read_participation_for_window(workbook, date(2026, 1, 1), date(2026, 4, 30))
    assert set(out["Poll Id"]) == {"12345"}


def test_read_participation_for_window_handles_empty_tab():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=pd.DataFrame()):
        out = sheets.read_participation_for_window(workbook, date(2026, 4, 1), date(2026, 4, 30))
    assert out.empty


def test_read_participation_for_window_header_only_tab():
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws

    header_only = pd.DataFrame(columns=["Poll Id", "Start Date", "End Date", "Title", "alpha"])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=header_only):
        out = sheets.read_participation_for_window(workbook, date(2026, 4, 1), date(2026, 4, 30))
    assert out.empty


# ---------------------------------------------------------------------------
# read_communication_master
# ---------------------------------------------------------------------------


def test_read_communication_master_missing_tab_raises():
    workbook = MagicMock()
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    with pytest.raises(RuntimeError, match="missing the 'Communication Master' tab"):
        sheets.read_communication_master(workbook)


def test_read_communication_master_empty_tab_returns_empty_df(empty_existing_ws):
    workbook, _ = empty_existing_ws
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=pd.DataFrame()):
        out = sheets.read_communication_master(workbook)
    assert isinstance(out, pd.DataFrame)
    assert out.empty
    assert "Communication Status" in out.columns


def test_read_communication_master_header_only_returns_empty(empty_existing_ws):
    workbook, _ = empty_existing_ws
    header_only = pd.DataFrame(columns=["Poll Id", "Start Date", "End Date", "Title", "alpha"])
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=header_only):
        out = sheets.read_communication_master(workbook)
    assert out.empty


def test_read_communication_master_pivots_into_long_dataframe(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = pd.DataFrame({
        "Poll Id": ["12345", "67890"],
        "Start Date": ["2026-04-05", "2026-04-12"],
        "End Date": ["2026-04-07", "2026-04-14"],
        "Title": ["Poll A", "Poll B"],
        "alpha": ["Yes", ""],
        "beta": ["No", "Pending verification"],
    })
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_communication_master(workbook)
    # 2 polls x 2 delegates = 4 long rows
    assert len(out) == 4
    alpha_12345 = out[(out["Delegate"] == "alpha") & (out["Poll Id"] == "12345")].iloc[0]
    assert alpha_12345["Communication Status"] == "Yes"
    assert alpha_12345["Start Date"] == date(2026, 4, 5)
    beta_67890 = out[(out["Delegate"] == "beta") & (out["Poll Id"] == "67890")].iloc[0]
    assert beta_67890["Communication Status"] == "Pending verification"


def test_read_communication_master_skips_blank_poll_id_rows(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = pd.DataFrame({
        "Poll Id": ["12345", "", "  "],
        "Start Date": ["2026-04-05", "", ""],
        "End Date": ["2026-04-07", "", ""],
        "Title": ["Poll A", "", ""],
        "alpha": ["Yes", "stray", "stray"],
    })
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_communication_master(workbook)
    assert set(out["Poll Id"]) == {"12345"}


def test_read_communication_master_no_delegate_columns_returns_empty(empty_existing_ws):
    workbook, _ = empty_existing_ws
    df = pd.DataFrame({
        "Poll Id": ["12345"],
        "Start Date": ["2026-04-05"],
        "End Date": ["2026-04-07"],
        "Title": ["Poll A"],
    })
    with patch("ad_voting_metrics.sheets.get_as_dataframe", return_value=df):
        out = sheets.read_communication_master(workbook)
    assert out.empty


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
