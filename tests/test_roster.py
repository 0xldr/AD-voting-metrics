"""Tests for the roster module: Delegate validation, YAML loading, drift detection, and period filtering."""

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import (
    Delegate,
    DelegatesConfig,
    build_roster_for_period,
    detect_roster_drift,
    load_delegates,
)
from tests.helpers import ADDR_A, ADDR_B, ADDR_C, delegate

_APRIL = MonthPeriod(2026, 4)


def _api_entry(name: str, address: str) -> dict:
    return {"name": name, "voteDelegateAddress": address}


def _write_roster(tmp_path: Path, *delegates: Delegate) -> Path:
    """Write the delegates as a roster YAML and return its path."""
    path = tmp_path / "delegates.yaml"
    path.write_text(yaml.safe_dump({"delegates": [d.model_dump() for d in delegates]}))
    return path


@pytest.mark.parametrize(
    "bad_address",
    [
        "0x1234567890ABCDef1234567890abcdef1234567",  # uppercase rejected
        "0x12345",  # too short
        "1234567890abcdef1234567890abcdef12345678",  # missing 0x prefix
    ],
)
def test_address_must_match_lowercase_hex_pattern(bad_address):
    with pytest.raises(ValidationError, match="String should match pattern"):
        delegate(address=bad_address)


def test_name_must_be_non_empty():
    with pytest.raises(ValidationError, match="at least 1 character"):
        delegate(name="   ")


@pytest.mark.parametrize("end", [date(2024, 12, 31), date(2025, 1, 1)], ids=["before start", "equal to start"])
def test_end_date_must_be_strictly_after_start(end):
    with pytest.raises(ValidationError, match=r"end_date.*must be after"):
        delegate(start=date(2025, 1, 1), end=end)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        pytest.param(date(2025, 1, 1), None, True, id="aligned before period, still active"),
        pytest.param(date(2026, 4, 15), None, True, id="aligned mid-period"),
        pytest.param(date(2025, 1, 1), date(2026, 4, 15), True, id="exited mid-period"),
        pytest.param(date(2026, 4, 30), None, True, id="aligned on last day of period"),
        pytest.param(date(2025, 1, 1), date(2026, 4, 1), True, id="exited on first day (end_date inclusive)"),
        pytest.param(date(2026, 5, 1), None, False, id="aligned after period"),
        pytest.param(date(2026, 5, 1), date(2026, 6, 1), False, id="aligned one day after period"),
        pytest.param(date(2025, 1, 1), date(2026, 3, 31), False, id="exited one day before period"),
    ],
)
def test_is_active_during_april_2026(start, end, expected):
    """Interval-overlap with the queried month; both alignment bounds inclusive."""
    assert delegate(start=start, end=end).is_active_during(_APRIL) is expected


def test_empty_roster_is_valid():
    assert DelegatesConfig(delegates=[]).delegates == []


def test_duplicate_addresses_rejected():
    with pytest.raises(ValidationError, match="Duplicate vote_delegate_address"):
        DelegatesConfig(delegates=[delegate("A", ADDR_A), delegate("B", ADDR_A)])


def test_load_delegates_parses_active_and_exited_entries(tmp_path):
    path = _write_roster(tmp_path, delegate("Alice", ADDR_A), delegate("Bob", ADDR_B, end=date(2024, 6, 30)))

    config = load_delegates(path)

    assert [d.name for d in config.delegates] == ["Alice", "Bob"]
    assert config.delegates[1].end_date == date(2024, 6, 30)


def test_load_delegates_rejects_empty_file(tmp_path):
    path = tmp_path / "delegates.yaml"
    path.write_text("")

    with pytest.raises(ValueError, match="empty"):
        load_delegates(path)


def test_committed_roster_loads():
    config = load_delegates(Path(__file__).resolve().parent.parent / "delegates.yaml")

    assert config.delegates


@pytest.mark.parametrize(
    ("yaml_delegates", "api", "expected_fragments"),
    [
        pytest.param([delegate("Active", ADDR_A)], [_api_entry("active", ADDR_A)], [], id="match by address, not name"),
        pytest.param(
            [delegate("Active", ADDR_A)],
            [_api_entry("Active", ADDR_A.upper().replace("0X", "0x"))],
            [],
            id="API address case is ignored",
        ),
        pytest.param([delegate("Ghost", ADDR_A)], [], ["Ghost", "active in YAML"], id="YAML active, API absent"),
        pytest.param([delegate("Gone", ADDR_A, end=date(2025, 6, 30))], [], [], id="YAML exited, API absent"),
        pytest.param(
            [delegate("Back", ADDR_A, end=date(2025, 6, 30))],
            [_api_entry("Back", ADDR_A)],
            ["Back", "exited in YAML"],
            id="YAML exited, API present",
        ),
        pytest.param([], [_api_entry("New", ADDR_B)], ["New", "not in delegates.yaml"], id="API present, not in YAML"),
    ],
)
def test_detect_roster_drift(yaml_delegates, api, expected_fragments):
    warnings = detect_roster_drift(DelegatesConfig(delegates=yaml_delegates), api)

    assert len(warnings) == (1 if expected_fragments else 0)
    for fragment in expected_fragments:
        assert fragment in warnings[0]


def test_build_roster_for_period_filters_to_delegates_active_in_period(tmp_path):
    path = _write_roster(
        tmp_path,
        delegate("Active", ADDR_A),
        delegate("ExitedBefore", ADDR_B, start=date(2023, 1, 1), end=date(2025, 12, 31)),
        delegate("AlignedAfter", ADDR_C, start=date(2027, 1, 1)),
    )

    def api():
        return [_api_entry("Active", ADDR_A), _api_entry("AlignedAfter", ADDR_C)]

    result = build_roster_for_period(path, _APRIL, api)

    assert [d.name for d in result.active_delegates] == ["Active"]
    assert result.drift_warnings == []
    assert result.api_delegate_count == 2
    assert len(result.yaml_config.delegates) == 3


def test_build_roster_for_period_reports_drift(tmp_path):
    path = _write_roster(tmp_path, delegate("Active", ADDR_A))

    def api():
        return []

    result = build_roster_for_period(path, _APRIL, api)

    assert len(result.drift_warnings) == 1
    assert "Active" in result.drift_warnings[0]


def test_build_roster_for_period_soft_fails_when_api_fetch_raises(tmp_path):
    path = _write_roster(tmp_path, delegate("Active", ADDR_A))

    def api():
        raise ConnectionError("network is down")

    result = build_roster_for_period(path, _APRIL, api)

    assert [d.name for d in result.active_delegates] == ["Active"]
    assert len(result.drift_warnings) == 1
    assert "API drift check skipped" in result.drift_warnings[0]
    assert "network is down" in result.drift_warnings[0]
    assert result.api_delegate_count is None
