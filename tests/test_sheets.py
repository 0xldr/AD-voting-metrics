"""Tests for the sheets module — auth + workbook connection.

Unit tests mock gspread and google-auth so they don't hit live Google. The
single integration test (marked @pytest.mark.integration) is skipped by
default and runs only with `pytest -m integration`; it requires real env
vars and verifies end-to-end connectivity.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import gspread
import pytest

from ad_voting_metrics import sheets
from ad_voting_metrics.period import MonthPeriod

# ---------------------------------------------------------------------------
# SCOPES — pinned so an accidental edit fails the test
# ---------------------------------------------------------------------------


def test_scopes_are_sheets_and_drive():
    """The Sheets scope is for cell I/O; the Drive scope is required for
    gspread's open_by key. Both are needed.
    """
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
    """Without the workbook id env var and without an explicit arg, fail clearly.
    The service-account env var is set so we exercise the second branch.
    """
    fake_sa = tmp_path / "fake.json"
    fake_sa.write_text("{}")
    monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_FILE", str(fake_sa))
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="SHEETS_WORKBOOK_ID"):
        sheets.get_workbook()


def test_get_workbook_both_env_vars_missing_raises_service_account_first(monkeypatch):
    """When both are missing, the service-account error comes first.
    Documents the check order; operator sees them in sequence."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    monkeypatch.delenv("SHEETS_WORKBOOK_ID", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_SERVICE_ACCOUNT_FILE"):
        sheets.get_workbook()


def test_get_workbook_error_message_points_at_env_example(monkeypatch):
    """The error message tells the operator where to look for the fix."""
    monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_FILE", raising=False)
    with pytest.raises(RuntimeError, match=".env.example"):
        sheets.get_workbook(workbook_id="anything")


# ---------------------------------------------------------------------------
# get_workbook — env var validation
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
    """Path exists but is a directory, not a file. Surfacing this
    separately helps the operator see the problem clearly.
    """
    with pytest.raises(RuntimeError, match="not a file"):
        sheets.get_workbook(
            service_account_file=tmp_path,
            workbook_id="anything",
        )


# ---------------------------------------------------------------------------
# get_workbook —credentials parsing
# ---------------------------------------------------------------------------


def test_get_workbook_malformed_json_raises(tmp_path):
    """File exists but isn't valid service-account JSON. google-auth
    raises ValueError; we wrap it as RuntimeError with context.
    """
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not valid json}")
    with pytest.raises(RuntimeError, match="could not be parsed"):
        sheets.get_workbook(
            service_account_file=bad_file,
            workbook_id="anything",
        )


def test_get_workbook_wrong_key_type_raises(tmp_path):
    """Valid JSON but not a service-account key (e.g., user credentials
    or a random object). google-auth's from_service_account_file rejects
    these with ValueError too.
    """
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
    """Write a syntactically valid service- account JSON to a tmp file.

    The fields are dummies - we patch out the actual credential loading,
    so the file content only needs to be parseable JSON for path checks
    to pass. We don't need real RSA keys.
    """
    sa = tmp_path / "sa.json"
    sa.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "fake-project",
                "client_email": "fake@fake-project.iam.gserviceaccount.com",
            }
        )
    )
    return sa


def test_get_workbook_happy_path_returns_spreadsheet(tmp_path):
    """When everything works, return the gspread spreadsheet."""
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
    """Credentials.from_service_account_file gets scopes from sheets.SCOPES.
    Catches accidental scope edits at the wrong layer.
    """
    sa_file = _make_fake_sa_file(tmp_path)

    with (
        patch.object(
            sheets.Credentials,
            "from_service_account_file",
        ) as mock_from_file,
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
    """gspread raises APIError when the workbook is not accessible (wrong
    ID, not shared, etc.). We wrap with a helpful message naming the
    service account email so the operator can share the workbook.
    """
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
    """The wrapped error message includes the service account email so the
    operator knows exactly what to add to the workbook's sharing list."""
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
        pytest.raises(RuntimeError, match="scripted@my-project.iam.gserviceaccount.com"),
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
    and SHEETS_WORKBOOK_ID set in the environment (loaded from .env if
    using python-dotenv).

    Verifies that auth works, the workbook is reachable, and the service
    account has at least read access. Doesn't modify the workbook.
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
    """An (impossible in practice — every Sheets workbook has at least one
    tab) but the function should handle it without surprise."""
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
    """Existing tab is returned regardless of the rows/cols passed.
    Resizing on every call would shrink/grow tabs unpredictably."""
    existing_ws = MagicMock(spec=gspread.Worksheet)
    workbook = MagicMock()
    workbook.worksheet.return_value = existing_ws

    sheets.get_or_create_tab(workbook, "Existing", rows=999, cols=99)

    # add_worksheet not called → no resize attempt
    workbook.add_worksheet.assert_not_called()
    # existing_ws.resize() should not be called either
    existing_ws.resize.assert_not_called()


def test_get_or_create_tab_requires_keyword_only_rows_cols():
    """rows and cols are keyword-only — passing positionally is a TypeError.
    Forces callers to be explicit about size."""
    workbook = MagicMock()
    workbook.worksheet.return_value = MagicMock(spec=gspread.Worksheet)

    with pytest.raises(TypeError):
        # Attempting positional args should fail
        sheets.get_or_create_tab(workbook, "Tab", 100, 10)  # type: ignore[misc]


def test_clear_tab_calls_worksheet_clear():
    """clear_tab delegates to gspread's worksheet.clear() which wipes
    values while preserving formatting."""
    worksheet = MagicMock(spec=gspread.Worksheet)

    sheets.clear_tab(worksheet)

    worksheet.clear.assert_called_once()


# ---------------------------------------------------------------------------
# Tab management — integration tests against the real workbook
# ---------------------------------------------------------------------------


@pytest.fixture
def _temp_tab(request):
    """Yield a unique temp-tab title; clean up the tab after the test.

    Used by integration tests that need to create a tab to exercise.
    The teardown deletes the tab even if the test failed, so we don't
    accumulate cruft in the test workbook.
    """
    import uuid

    tab_name = f"_test_temp_{uuid.uuid4().hex[:8]}"

    def cleanup():
        try:
            workbook = sheets.get_workbook()
            try:
                ws = workbook.worksheet(tab_name)
                workbook.del_worksheet(ws)
            except gspread.exceptions.WorksheetNotFound:
                pass  # tab was never created or already deleted
        except Exception:
            # Even if cleanup fails, don't mask the underlying test result.
            # Operator can manually delete leftover _test_temp_* tabs.
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
    """Create a real tab, verify it shows up, fetch it again, get
    the same one back. Teardown deletes it."""
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
    import pandas as pd

    return pd.DataFrame(
        {
            "Date": [date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 2)],
            "Delegate": ["BLUE", "Cloaky", "BLUE"],
            "Total Delegation": [1234567.89, 987654.32, 1234999.99],
            "Rank": [1, 2, 1],
        }
    )


def test_daily_data_columns_pinned():
    """The column tuple is pinned so an accidental edit fails the test."""
    assert sheets.DAILY_DATA_COLUMNS == ("Date", "Delegate", "Total Delegation", "Rank")


def test_daily_data_tab_title():
    """Tab title format: 'Daily Data {period}'. Matches MonthPeriod.__str__."""
    period = MonthPeriod(year=2026, month=4)
    assert sheets._daily_data_tab_title(period) == "Daily Data April 2026"


def test_write_daily_data_missing_columns_raises():
    """The dataframe must have at least the four expected columns."""
    import pandas as pd

    df_bad = pd.DataFrame({"Date": [date(2026, 4, 1)], "Delegate": ["BLUE"]})
    workbook = MagicMock()
    period = MonthPeriod(year=2026, month=4)

    with pytest.raises(ValueError, match="missing required columns"):
        sheets.write_daily_data(workbook, period, df_bad)


def test_write_daily_data_creates_tab_with_correct_title():
    """Tab title is 'Daily Data {period}'."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.side_effect = gspread.exceptions.WorksheetNotFound
    workbook.add_worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    # get_or_create_tab was called via add_worksheet (since worksheet raised)
    call = workbook.add_worksheet.call_args
    assert call.kwargs["title"] == "Daily Data April 2026"


def test_write_daily_data_clears_existing_tab_before_writing():
    """Re-runs overwrite: clear is called before update."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    # clear() called once on the existing worksheet
    fake_ws.clear.assert_called_once()
    # update() called once with values
    fake_ws.update.assert_called_once()


def test_write_daily_data_writes_header_and_rows():
    """The values written are header followed by data rows."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    update_kwargs = fake_ws.update.call_args.kwargs
    values = update_kwargs["values"]

    # First row is the header
    assert values[0] == ["Date", "Delegate", "Total Delegation", "Rank"]
    # Three data rows after header
    assert len(values) == 4
    # First data row matches our fixture (date as ISO string, numerics as native)
    assert values[1] == ["2026-04-01", "BLUE", 1234567.89, 1]
    assert values[2] == ["2026-04-01", "Cloaky", 987654.32, 2]
    assert values[3] == ["2026-04-02", "BLUE", 1234999.99, 1]


def test_write_daily_data_range_matches_data_shape():
    """The A1 range passed to update() matches the values shape exactly."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    update_kwargs = fake_ws.update.call_args.kwargs
    # 4 columns (A:D), 4 rows (header + 3 data rows)
    assert update_kwargs["range_name"] == "A1:D4"


def test_write_daily_data_handles_pandas_timestamps():
    """Dates can arrive as pandas Timestamps; they get ISO-formatted too."""
    import pandas as pd

    df = pd.DataFrame(
        {
            "Date": pd.to_datetime(["2026-04-01", "2026-04-02"]),
            "Delegate": ["BLUE", "Cloaky"],
            "Total Delegation": [100.0, 200.0],
            "Rank": [1, 2],
        }
    )
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    # Date strings should be ISO format (Timestamp.isoformat() includes time;
    # we don't strip it here but it should at least start with the date)
    assert values[1][0].startswith("2026-04-01")
    assert values[2][0].startswith("2026-04-02")


def test_write_daily_data_extra_columns_ignored():
    """If df_ranking has extra columns beyond the four required, they're
    ignored — only the canonical four are written."""
    import pandas as pd

    df = pd.DataFrame(
        {
            "Date": [date(2026, 4, 1)],
            "Delegate": ["BLUE"],
            "Total Delegation": [100.0],
            "Rank": [1],
            "ExtraColumn": ["ignored"],
        }
    )
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values[0] == ["Date", "Delegate", "Total Delegation", "Rank"]
    assert len(values[1]) == 4
    assert "ignored" not in values[1]


def test_write_daily_data_empty_dataframe_writes_header_only():
    """An empty df_ranking writes just the header row."""
    import pandas as pd

    df = pd.DataFrame(columns=["Date", "Delegate", "Total Delegation", "Rank"])
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    sheets.write_daily_data(workbook, period, df)

    values = fake_ws.update.call_args.kwargs["values"]
    assert values == [["Date", "Delegate", "Total Delegation", "Rank"]]
    assert fake_ws.update.call_args.kwargs["range_name"] == "A1:D1"


def test_write_daily_data_returns_worksheet():
    """The function returns the worksheet for caller convenience."""
    df = _make_ranking_df()
    workbook = MagicMock()
    fake_ws = MagicMock(spec=gspread.Worksheet)
    workbook.worksheet.return_value = fake_ws
    period = MonthPeriod(year=2026, month=4)

    result = sheets.write_daily_data(workbook, period, df)

    assert result is fake_ws


# ---------------------------------------------------------------------------
# write_daily_data — integration test against the real workbook
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_write_daily_data_to_real_workbook(_temp_tab, monkeypatch):
    """End-to-end: write a small Daily Data set, read it back, verify
    cells match. Uses a temp tab via monkeypatching the tab-title helper."""
    import pandas as pd

    df = pd.DataFrame(
        {
            "Date": [date(2026, 4, 1), date(2026, 4, 2)],
            "Delegate": ["TestDelegateA", "TestDelegateB"],
            "Total Delegation": [123.45, 678.90],
            "Rank": [1, 2],
        }
    )

    # Force the writer to use our temp tab name instead of "Daily Data ..."
    monkeypatch.setattr(sheets, "_daily_data_tab_title", lambda period: _temp_tab)

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
    # Numeric cells come back as strings via acell().value; cast for comparison
    # Assert non-None first so pyright narrows from `str | None` to `str`.
    c2 = ws.acell("C2").value
    assert c2 is not None
    assert float(c2) == 123.45
    d2 = ws.acell("D2").value
    assert d2 is not None
    assert int(d2) == 1
