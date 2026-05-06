"""Tests for the roster module - Delegate, DelegatesConfig, load_delegates."""

from datetime import date
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ad_voting_metrics.roster import Delegate, DelegatesConfig, load_delegates

# ---------------------------------------------------------------------------
# Delegate construction and validation
# ---------------------------------------------------------------------------


def test_construct_active_delegate():
    d = Delegate(
        name="Alice",
        voteDelegateAddress="0x1234567890abcdef1234567890abcdef12345678",
        startDate=date(2025, 1, 1),
        endDate=None,
    )
    assert d.name == "Alice"
    assert d.endDate is None


def test_construct_exited_delegate():
    d = Delegate(
        name="Bob",
        voteDelegateAddress="0xabcdef1234567890abcdef1234567890abcdef12",
        startDate=date(2024, 1, 1),
        endDate=date(2024, 6, 30),
    )
    assert d.endDate == date(2024, 6, 30)


def test_address_must_be_lowercase_hex():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Charlie",
            voteDelegateAddress="0x1234567890ABCDef1234567890abcdef1234567",
            startDate=date(2025, 1, 1),
        )


def test_address_must_be_40_hex_digits():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Dave",
            voteDelegateAddress="0x12345",
            startDate=date(2025, 1, 1),
        )


def test_address_must_have_0x_prefix():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Eve",
            voteDelegateAddress="1234567890abcdef1234567890abcdef12345678",
            startDate=date(2025, 1, 1),
        )


def test_name_must_be_non_empty():
    with pytest.raises(ValidationError, match="name must be non-empty"):
        Delegate(
            name="   ",
            voteDelegateAddress="0x1234567890abcdef1234567890abcdef12345678",
            startDate=date(2025, 1, 1),
        )


def test_end_date_must_be_after_start():
    with pytest.raises(ValidationError, match="endDate.*must be after"):
        Delegate(
            name="Frank",
            voteDelegateAddress="0x1234567890abcdef1234567890abcdef12345678",
            startDate=date(2025, 1, 1),
            endDate=date(2024, 12, 31),
        )


def test_end_date_equal_to_start_date_rejected():
    # endDate must be *strictly* after startDate, so equal dates should also be rejected.
    with pytest.raises(ValidationError, match="endDate.*must be after"):
        Delegate(
            name="Grace",
            voteDelegateAddress="0x1234567890abcdef1234567890abcdef12345678",
            startDate=date(2025, 1, 1),
            endDate=date(2025, 1, 1),
        )


# ---------------------------------------------------------------------------
# is_active_during — interval overlap with the queried month
# ---------------------------------------------------------------------------


def _delegate(start: date, end: date | None = None) -> Delegate:
    return Delegate(
        name="Harry",
        voteDelegateAddress="0x1234567890abcdef1234567890abcdef12345678",
        startDate=start,
        endDate=end,
    )


def test_active_aligned_before_period_no_end():
    # Aligned before, still active
    d = _delegate(start=date(2025, 1, 1), end=None)
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_active_aligned_mid_period():
    # Aligned mid-period, still active
    d = _delegate(start=date(2026, 4, 15), end=None)
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_active_exited_mid_period():
    # Aligned before, exited during
    d = _delegate(start=date(2025, 1, 1), end=date(2026, 4, 15))
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_active_full_overlap_with_end_date():
    # Aligned before, exited after
    d = _delegate(start=date(2025, 1, 1), end=date(2026, 5, 15))
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_inactive_aligned_after_period():
    # Aligned after period ends
    d = _delegate(start=date(2026, 5, 1), end=None)
    assert not d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_inactive_exited_before_period():
    # Aligned before, exited before period starts
    d = _delegate(start=date(2025, 1, 1), end=date(2026, 3, 31))
    assert not d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_boundary_aligned_on_last_day_of_period():
    # Aligned on the last day, should be active
    d = _delegate(start=date(2026, 4, 30))
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_boundary_exited_on_first_day_of_period():
    # endDate is inclusive, so should be active
    d = _delegate(start=date(2025, 1, 1), end=date(2026, 4, 1))
    assert d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_boundary_aligned_one_day_after_period():
    # Aligned the day after, inactive
    d = _delegate(start=date(2026, 5, 1), end=date(2026, 6, 1))
    assert not d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


def test_boundary_exited_one_day_before_period():
    # Exited the day before, inactive
    d = _delegate(start=date(2025, 1, 1), end=date(2026, 3, 31))
    assert not d.is_active_during(date(2026, 4, 1), date(2026, 4, 30))


# ---------------------------------------------------------------------------
# DelegatesConfig validation
# ---------------------------------------------------------------------------


def test_empty_list_accepted():
    # An empty list is valid. Drift detection
    # will warn if the API returns delegates.
    config = DelegatesConfig(delegates=[])
    assert config.delegates == []


def test_duplicate_addresses_rejected():
    addr = "0x1234567890abcdef1234567890abcdef12345678"
    with pytest.raises(ValidationError, match="Duplicate voteDelegateAddress"):
        DelegatesConfig(
            delegates=[
                Delegate(name="A", voteDelegateAddress=addr, startDate=date(2025, 1, 1)),
                Delegate(name="B", voteDelegateAddress=addr, startDate=date(2025, 2, 1)),
            ]
        )


# ---------------------------------------------------------------------------
# load_delegates — file IO
# ---------------------------------------------------------------------------


def test_load_delegates_happy_path(tmp_path):
    yaml_text = """
    delegates:
      - name: Alice
        voteDelegateAddress: "0x1234567890abcdef1234567890abcdef12348899"
        startDate: 2025-01-01
        endDate: null
        """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)
    config = load_delegates(p)
    assert len(config.delegates) == 1
    assert config.delegates[0].name == "Alice"


def test_load_delegates_with_exited_delegate(tmp_path):
    yaml_text = """
    delegates:
      - name: Alice
        voteDelegateAddress: "0x1234567890abcdef1234567890abcdef12348899"
        startDate: 2025-01-01
        endDate: null
      - name: Bob
        voteDelegateAddress: "0xabcdef1234567890abcdef1234567890abcdef12"
        startDate: 2024-01-01
        endDate: 2024-06-30
        """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)
    config = load_delegates(p)
    assert len(config.delegates) == 2
    assert config.delegates[1].endDate == date(2024, 6, 30)


def test_load_delegates_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_delegates(tmp_path / "nonexistent.yaml")


def test_load_delegates_empty_file(tmp_path):
    p = tmp_path / "delegates.yaml"
    p.write_text("")
    with pytest.raises(ValueError, match="empty"):
        load_delegates(p)


def test_load_delegates_malformed_yaml(tmp_path):
    p = tmp_path / "delegates.yaml"
    p.write_text("delegates:\n  - name: X\n   bad indent: y\n")
    with pytest.raises(yaml.YAMLError):
        load_delegates(p)


def test_load_delegates_schema_violation(tmp_path):
    yaml_text = """
    delegates:
    - name: X
      voteDelegateAddress: "not-an-address"
      startDate: 2024-01-01
"""
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)
    with pytest.raises(ValidationError):
        load_delegates(p)


# ---------------------------------------------------------------------------
# Sanity check the actual delegates.yaml in the repo
# ---------------------------------------------------------------------------


def test_real_delegates_yaml():
    """The committed delegates.yaml at the repo root must load without errors."""

    repo_root = Path(__file__).resolve().parent.parent
    yaml_path = repo_root / "delegates.yaml"
    config = load_delegates(yaml_path)
    assert len(config.delegates) > 0
    for d in config.delegates:
        assert d.name
        assert d.voteDelegateAddress.startswith("0x")
        assert len(d.voteDelegateAddress) == 42
