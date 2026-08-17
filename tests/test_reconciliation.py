"""Tests for the reconciliation log module.

build_entry produces a JSON-serializable dict; write_entry writes one
JSON file per run to output_data/reconciliation/, named by period and
run timestamp, with soft-fail on errors.
"""

import json
from datetime import date, datetime
from pathlib import Path
from typing import cast

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.reconciliation import ReconciliationEntry, build_entry, write_entry
from ad_voting_metrics.roster import Delegate, DelegatesConfig, RosterResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_yaml_config(active: int = 1, exited: int = 0) -> DelegatesConfig:
    """Return a DelegatesConfig with the given counts of active/exited delegates."""
    active_delegates = [
        Delegate(
            name=f"Active{i}",
            vote_delegate_address=f"0x{i:040x}",
            start_date=date(2024, 1, 1),
            end_date=None,
        )
        for i in range(active)
    ]
    exited_delegates = [
        Delegate(
            name=f"Exited{i}",
            vote_delegate_address=f"0xff{i:038x}",
            start_date=date(2023, 1, 1),
            end_date=date(2025, 6, 30),
        )
        for i in range(exited)
    ]
    return DelegatesConfig(delegates=[*active_delegates, *exited_delegates])


def _sample_period() -> MonthPeriod:
    return MonthPeriod(2026, 4)


def _make_roster_result(
    yaml_config: DelegatesConfig | None = None,
    *,
    active_delegates: list[Delegate] | None = None,
    drift_warnings: list[str] | None = None,
    api_delegate_count: int = 0,
    api_fetch_succeeded: bool = True,
) -> RosterResult:
    """Build a RosterResult for build_entry tests.

    Returns:
        A RosterResult populated from the args / defaults.
    """
    cfg = yaml_config if yaml_config is not None else _make_yaml_config()
    return RosterResult(
        active_delegates=active_delegates if active_delegates is not None else [],
        drift_warnings=drift_warnings if drift_warnings is not None else [],
        yaml_config=cfg,
        api_delegate_count=api_delegate_count,
        api_fetch_succeeded=api_fetch_succeeded,
    )


def _make_entry(**overrides: object) -> ReconciliationEntry:
    """Return a complete ReconciliationEntry with sensible defaults.

    Tests override only the fields they assert on; the rest come from the base. Overrides are typed `object` because
    ReconciliationEntry's fields are heterogeneous and pyright treats the merged dict as `dict[str, object]` until
    cast back.
    """
    base: ReconciliationEntry = {
        "run_timestamp": "2026-05-06T15:32:08+00:00",
        "period": "April 2026",
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
        "yaml_path": "/tmp/delegates.yaml",
        "yaml_total_delegates": 0,
        "yaml_active_delegates": 0,
        "yaml_exited_delegates": 0,
        "api_delegate_count": 0,
        "api_fetch_succeeded": True,
        "active_during_period": 0,
        "drift_warnings": [],
        "delegation_source": "onchain",
        "delegation_factory_block": 22368737,
        "delegation_last_synced_block": 22500000,
        "output_files": [],
    }
    return cast("ReconciliationEntry", {**base, **overrides})


# ---------------------------------------------------------------------------
# build_entry — dict shape and field correctness
# ---------------------------------------------------------------------------


def test_build_entry_minimal():
    """Required fields are populated with sensible types and values."""
    period = _sample_period()
    yaml_config = _make_yaml_config(active=2, exited=1)
    active_delegates = yaml_config.delegates[:2]

    entry = build_entry(
        period=period,
        yaml_path=Path("/tmp/delegates.yaml"),
        roster=_make_roster_result(
            yaml_config,
            active_delegates=active_delegates,
            api_delegate_count=2,
        ),
        delegation={"factory_block": 22368737, "last_synced_block": 22500000},
        output_files=[Path("/tmp/output.csv")],
    )

    assert entry["period"] == "April 2026"
    assert entry["period_start"] == "2026-04-01"
    assert entry["period_end"] == "2026-04-30"
    assert entry["yaml_total_delegates"] == 3
    assert entry["yaml_active_delegates"] == 2
    assert entry["yaml_exited_delegates"] == 1
    assert entry["api_delegate_count"] == 2
    assert entry["api_fetch_succeeded"] is True
    assert entry["active_during_period"] == 2
    assert entry["drift_warnings"] == []
    assert entry["delegation_source"] == "onchain"
    assert entry["delegation_factory_block"] == 22368737
    assert entry["delegation_last_synced_block"] == 22500000
    assert entry["output_files"] == ["/tmp/output.csv"]


def test_build_entry_preserves_drift_warnings():
    entry = build_entry(
        period=_sample_period(),
        yaml_path=Path("/tmp/delegates.yaml"),
        roster=_make_roster_result(
            drift_warnings=["Cloaky vanished from API", "Mystery delegate appeared"],
        ),
        delegation={"factory_block": 22368737, "last_synced_block": 22500000},
        output_files=[],
    )

    assert entry["drift_warnings"] == [
        "Cloaky vanished from API",
        "Mystery delegate appeared",
    ]


def test_build_entry_records_api_failure():
    entry = build_entry(
        period=_sample_period(),
        yaml_path=Path("/tmp/delegates.yaml"),
        roster=_make_roster_result(
            drift_warnings=["API drift check skipped..."],
            api_fetch_succeeded=False,
        ),
        delegation={"factory_block": 22368737, "last_synced_block": 22500000},
        output_files=[],
    )

    assert entry["api_fetch_succeeded"] is False
    assert entry["api_delegate_count"] == 0


def test_build_entry_run_timestamp_is_iso_with_tz():
    """run_timestamp should be parseable as an ISO 8601 datetime in UTC."""
    entry = build_entry(
        period=_sample_period(),
        yaml_path=Path("/tmp/delegates.yaml"),
        roster=_make_roster_result(),
        delegation={"factory_block": 22368737, "last_synced_block": 22500000},
        output_files=[],
    )

    parsed = datetime.fromisoformat(entry["run_timestamp"])
    assert parsed.tzinfo is not None


def test_build_entry_is_json_serializable():
    """The entry must serialize to JSON without custom encoders."""
    yaml_cfg = _make_yaml_config(active=2, exited=1)
    entry = build_entry(
        period=_sample_period(),
        yaml_path=Path("/tmp/delegates.yaml"),
        roster=_make_roster_result(
            yaml_cfg,
            active_delegates=_make_yaml_config(active=2).delegates,
            drift_warnings=["w1", "w2"],
            api_delegate_count=2,
        ),
        delegation={"factory_block": 22368737, "last_synced_block": 22500000},
        output_files=[Path("/o/a.csv")],
    )

    serialized = json.dumps(entry)
    round_tripped = json.loads(serialized)
    assert round_tripped == entry


# ---------------------------------------------------------------------------
# write_entry — file IO, filename format, and soft-fail
# ---------------------------------------------------------------------------


def test_write_entry_creates_file_with_period_and_timestamp_in_name(tmp_path):
    period = _sample_period()
    entry = _make_entry(run_timestamp="2026-05-06T15:32:08+00:00")

    path = write_entry(tmp_path, period, entry)

    assert path is not None
    # Filename starts with period (YYYY-MM), then run timestamp with hyphens
    # in place of colons.
    assert path.name == "2026-04_2026-05-06T15-32-08Z.json"
    assert path.exists()


def test_write_entry_serializes_entry_as_json(tmp_path):
    period = _sample_period()
    entry = _make_entry(run_timestamp="2026-05-06T15:32:08+00:00")

    path = write_entry(tmp_path, period, entry)
    assert path is not None

    parsed = json.loads(path.read_text())
    assert parsed == entry


def test_write_entry_creates_directory_if_missing(tmp_path):
    period = _sample_period()
    entry = _make_entry()

    target_dir = tmp_path / "deep" / "nested" / "reconciliation"
    assert not target_dir.exists()

    path = write_entry(target_dir, period, entry)

    assert path is not None
    assert path.exists()
    assert target_dir.is_dir()


def test_write_entry_distinct_filenames_for_same_period_different_timestamps(tmp_path):
    """Re-runs of the same period produce distinct files, not overwrites."""
    period = _sample_period()
    entry1 = _make_entry(run_timestamp="2026-05-06T15:32:08+00:00")
    entry2 = _make_entry(run_timestamp="2026-05-06T16:00:00+00:00")

    path1 = write_entry(tmp_path, period, entry1)
    path2 = write_entry(tmp_path, period, entry2)

    assert path1 is not None
    assert path2 is not None
    assert path1 != path2
    assert path1.exists()
    assert path2.exists()


def test_write_entry_soft_fails_on_io_error(caplog, monkeypatch):
    """Permission errors etc. are logged but not raised."""

    def boom(*args, **kwargs):
        raise PermissionError("no write access")

    monkeypatch.setattr(Path, "mkdir", boom)

    with caplog.at_level("WARNING"):
        # Must not raise
        result = write_entry(
            Path("/anywhere/reconciliation"),
            _sample_period(),
            _make_entry(),
        )

    assert result is None
    assert any("Failed to write reconciliation log" in r.message for r in caplog.records)


def test_write_entry_handles_unicode(tmp_path):
    """Non-ASCII content survives the round-trip (ensure_ascii=False is set)."""
    period = _sample_period()
    # Put unicode in schema-valid fields: yaml_path (paths can be unicode)
    # and drift_warnings (warning messages can name unicode-named delegates).
    entry = _make_entry(
        yaml_path="/tmp/Cüstom_Délégates.yaml",
        drift_warnings=["✓ verified: Cüstom Délégate"],
    )

    path = write_entry(tmp_path, period, entry)
    assert path is not None

    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert parsed["yaml_path"] == "/tmp/Cüstom_Délégates.yaml"
    assert parsed["drift_warnings"] == ["✓ verified: Cüstom Délégate"]


def test_write_entry_filename_sanitizes_iso_timestamp(tmp_path):
    """ISO timestamp is preserved with colons replaced by hyphens and +00:00 collapsed to Z."""
    period = _sample_period()
    entry = _make_entry(run_timestamp="2026-05-06T15:32:08.123456+00:00")

    path = write_entry(tmp_path, period, entry)
    assert path is not None
    assert path.name == "2026-04_2026-05-06T15-32-08.123456Z.json"
