"""Tests for the roster module - Delegate, DelegatesConfig, load_delegates."""

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
    load_delegates,
    merge_with_api,
    to_dataframe,
)

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


# ---------------------------------------------------------------------------
# merge_with_api — drift detection between YAML and API
# ---------------------------------------------------------------------------


def _api_entry(name: str, address: str) -> dict:
    """Minimal API-shaped delegate dict."""
    return {
        "name": name,
        "voteDelegateAddress": address,
        "status": "aligned",
    }


def test_merge_no_drift():
    addr = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(name="Active", voteDelegateAddress=addr, startDate=date(2024, 1, 1)),
        ]
    )
    api = [_api_entry("Active", addr)]
    delegates, warnings = merge_with_api(yaml_config, api)
    assert len(delegates) == 1
    assert warnings == []


def test_merge_yaml_active_api_absent_warns():
    addr = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(name="GhostlyActive", voteDelegateAddress=addr, startDate=date(2024, 1, 1)),
        ]
    )
    api: list[dict] = []  # API doesn't return this delegate
    _, warnings = merge_with_api(yaml_config, api)
    assert len(warnings) == 1
    assert "GhostlyActive" in warnings[0]
    assert "active in YAML" in warnings[0]


def test_merge_yaml_exited_api_absent_no_warn():
    """Expected case: YAML says exited, API doesn't return them."""
    addr = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(
                name="LegitimatelyExited",
                voteDelegateAddress=addr,
                startDate=date(2024, 1, 1),
                endDate=date(2025, 6, 30),
            ),
        ]
    )
    api: list[dict] = []
    _, warnings = merge_with_api(yaml_config, api)
    assert warnings == []


def test_merge_yaml_exited_api_present_warns():
    addr = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(
                name="ExitedButReappearing",
                voteDelegateAddress=addr,
                startDate=date(2024, 1, 1),
                endDate=date(2025, 6, 30),
            ),
        ]
    )
    api = [_api_entry("ExitedButReappearing", addr)]
    _, warnings = merge_with_api(yaml_config, api)
    assert len(warnings) == 1
    assert "exited in YAML" in warnings[0]


def test_merge_api_present_not_in_yaml_warns():
    yaml_config = DelegatesConfig(delegates=[])
    api = [_api_entry("NewlyAligned", "0x0f23de72e1581857eacd6308aebb69cf3a49cc86")]
    _, warnings = merge_with_api(yaml_config, api)
    assert len(warnings) == 1
    assert "NewlyAligned" in warnings[0]
    assert "not in delegates.yaml" in warnings[0]


def test_merge_address_case_insensitive():
    """API may return mixed-case addresses; comparison should still work."""
    addr_lower = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    addr_mixed = "0xFc48fBcA739079aaB08216C4d5E506B96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(name="X", voteDelegateAddress=addr_lower, startDate=date(2024, 1, 1)),
        ]
    )
    api = [_api_entry("X", addr_mixed)]
    _, warnings = merge_with_api(yaml_config, api)
    assert warnings == []


def test_merge_names_differ_addresses_match_no_warn():
    """Casing differences in names are intentional; don't flag them."""
    addr = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(name="BONAPUBLICA", voteDelegateAddress=addr, startDate=date(2024, 1, 1)),
        ]
    )
    api = [_api_entry("Bonapublica", addr)]  # different casing
    _, warnings = merge_with_api(yaml_config, api)
    assert warnings == []


def test_merge_returns_yaml_delegates():
    """The roster returned is the YAML's; API doesn't add anyone."""
    addr_in_yaml = "0xfc48fbca739079aab08216c4d5e506b96593753d"
    addr_in_api_only = "0x0f23de72e1581857eacd6308aebb69cf3a49cc86"
    yaml_config = DelegatesConfig(
        delegates=[
            Delegate(
                name="OnlyInYaml", voteDelegateAddress=addr_in_yaml, startDate=date(2024, 1, 1)
            ),
        ]
    )
    api = [_api_entry("OnlyInApi", addr_in_api_only)]
    delegates, _ = merge_with_api(yaml_config, api)
    # API-only entry produces a warning but is NOT added to the returned roster
    assert len(delegates) == 1
    assert delegates[0].name == "OnlyInYaml"


# ---------------------------------------------------------------------------
# build_roster_for_period — load + merge + filter
# ---------------------------------------------------------------------------


def test_build_roster_for_period_filters_to_active(tmp_path):
    """Only delegates active during the period are returned."""
    yaml_text = """
    delegates:
      - name: Active
        voteDelegateAddress: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        startDate: 2024-01-01
        endDate: null
      - name: ExitedBefore
        voteDelegateAddress: "0x0f23de72e1581857eacd6308aebb69cf3a49cc86"
        startDate: 2023-01-01
        endDate: 2025-12-31
      - name: AlignedAfter
        voteDelegateAddress: "0x173a1c04b79ed9266721c1154daa29addc0b9558"
        startDate: 2027-01-01
        endDate: null
    """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)

    period = MonthPeriod(2026, 4)
    fake_fetcher = lambda: [  # noqa: E731
        _api_entry("Active", "0xfc48fbca739079aab08216c4d5e506b96593753d"),
        _api_entry("AlignedAfter", "0x173a1c04b79ed9266721c1154daa29addc0b9558"),
    ]

    result = build_roster_for_period(p, period, fake_fetcher)

    # Only Active is in the period (ExitedBefore exited Dec 2025; AlignedAfter starts 2027)
    assert len(result.active_delegates) == 1
    assert result.active_delegates[0].name == "Active"


def test_build_roster_for_period_propagates_warnings(tmp_path):
    yaml_text = """
    delegates:
      - name: Active
        voteDelegateAddress: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        startDate: 2024-01-01
        endDate: null
    """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)

    # API doesn't return Active — should warn
    fake_fetcher = list

    result = build_roster_for_period(p, MonthPeriod(2026, 4), fake_fetcher)
    assert len(result.drift_warnings) == 1
    assert "Active" in result.drift_warnings[0]


def test_build_roster_for_period_soft_fails_on_api_error(tmp_path):
    """If the API fetch raises, drift detection is skipped with one warning."""
    yaml_text = """
    delegates:
      - name: Active
        voteDelegateAddress: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        startDate: 2024-01-01
        endDate: null
    """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)

    def failing_fetcher():
        raise ConnectionError("network is down")

    result = build_roster_for_period(p, MonthPeriod(2026, 4), failing_fetcher)

    # The roster still loads from YAML — soft fail
    assert len(result.active_delegates) == 1
    assert result.active_delegates[0].name == "Active"
    # One warning explaining the skipped check
    assert len(result.drift_warnings) == 1
    assert "API drift check skipped" in result.drift_warnings[0]
    assert "network is down" in result.drift_warnings[0]
    # api_fetch_succeeded reflects the failure
    assert result.api_fetch_succeeded is False
    assert result.api_delegate_count == 0


def test_build_roster_for_period_records_api_metadata(tmp_path):
    """RosterResult exposes the API count and success flag for the reconciliation log."""
    yaml_text = """
    delegates:
      - name: Active
        voteDelegateAddress: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        startDate: 2024-01-01
        endDate: null
    """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)

    fake_fetcher = lambda: [  # noqa: E731
        _api_entry("Active", "0xfc48fbca739079aab08216c4d5e506b96593753d"),
        _api_entry("Other", "0x0f23de72e1581857eacd6308aebb69cf3a49cc86"),
    ]

    result = build_roster_for_period(p, MonthPeriod(2026, 4), fake_fetcher)

    assert result.api_fetch_succeeded is True
    assert result.api_delegate_count == 2  # the API returned 2 entries
    # yaml_config exposed for downstream callers (reconciliation log)
    assert len(result.yaml_config.delegates) == 1


# ---------------------------------------------------------------------------
# to_dataframe — DataFrame shape compatible with sky_dao
# ---------------------------------------------------------------------------


def test_to_dataframe_columns():
    """sky_dao reads exactly Delegate Name, Delegate Contract, Start Date."""
    delegates = [
        Delegate(
            name="Cloaky",
            voteDelegateAddress="0x0f23de72e1581857eacd6308aebb69cf3a49cc86",
            startDate=date(2023, 6, 6),
        ),
    ]
    df = to_dataframe(delegates)
    assert list(df.columns) == ["Delegate Name", "Delegate Contract", "Start Date"]


def test_to_dataframe_start_date_is_string():
    """sky_dao parses Start Date with strptime, so it must be a string in '%Y-%m-%d'."""
    delegates = [
        Delegate(
            name="X",
            voteDelegateAddress="0xfc48fbca739079aab08216c4d5e506b96593753d",
            startDate=date(2024, 7, 4),
        ),
    ]
    df = to_dataframe(delegates)
    assert df.iloc[0]["Start Date"] == "2024-07-04"


def test_to_dataframe_preserves_address_format():
    delegates = [
        Delegate(
            name="X",
            voteDelegateAddress="0xfc48fbca739079aab08216c4d5e506b96593753d",
            startDate=date(2024, 1, 1),
        ),
    ]
    df = to_dataframe(delegates)
    assert df.iloc[0]["Delegate Contract"] == "0xfc48fbca739079aab08216c4d5e506b96593753d"


def test_to_dataframe_empty():
    df = to_dataframe([])
    assert list(df.columns) == ["Delegate Name", "Delegate Contract", "Start Date"]
    assert len(df) == 0
