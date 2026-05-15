"""Tests for the roster module - Delegate, DelegatesConfig, load_delegates."""

from datetime import date
from pathlib import Path
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from ad_voting_metrics.period import MonthPeriod
from ad_voting_metrics.roster import (
    Delegate,
    DelegatesConfig,
    LevelAssignment,
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
        vote_delegate_address="0x1234567890abcdef1234567890abcdef12345678",
        start_date=date(2025, 1, 1),
        end_date=None,
    )
    assert d.name == "Alice"
    assert d.end_date is None


def test_construct_exited_delegate():
    d = Delegate(
        name="Bob",
        vote_delegate_address="0xabcdef1234567890abcdef1234567890abcdef12",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 6, 30),
    )
    assert d.end_date == date(2024, 6, 30)


def test_address_must_be_lowercase_hex():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Charlie",
            vote_delegate_address="0x1234567890ABCDef1234567890abcdef1234567",
            start_date=date(2025, 1, 1),
        )


def test_address_must_be_40_hex_digits():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Dave",
            vote_delegate_address="0x12345",
            start_date=date(2025, 1, 1),
        )


def test_address_must_have_0x_prefix():
    with pytest.raises(ValidationError, match="String should match pattern"):
        Delegate(
            name="Eve",
            vote_delegate_address="1234567890abcdef1234567890abcdef12345678",
            start_date=date(2025, 1, 1),
        )


def test_name_must_be_non_empty():
    with pytest.raises(ValidationError, match="name must be non-empty"):
        Delegate(
            name="   ",
            vote_delegate_address="0x1234567890abcdef1234567890abcdef12345678",
            start_date=date(2025, 1, 1),
        )


def test_end_date_must_be_after_start():
    with pytest.raises(ValidationError, match=r"end_date.*must be after"):
        Delegate(
            name="Frank",
            vote_delegate_address="0x1234567890abcdef1234567890abcdef12345678",
            start_date=date(2025, 1, 1),
            end_date=date(2024, 12, 31),
        )


def test_end_date_equal_to_start_date_rejected():
    # end_date must be *strictly* after start_date, so equal dates should also be rejected.
    with pytest.raises(ValidationError, match=r"end_date.*must be after"):
        Delegate(
            name="Grace",
            vote_delegate_address="0x1234567890abcdef1234567890abcdef12345678",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 1),
        )


# ---------------------------------------------------------------------------
# LevelAssignment — schema and validation
# ---------------------------------------------------------------------------


def _delegate_with_levels(
    *,
    levels: list[dict] | None = None,
    name: str = "TestDelegate",
    vote_delegate_address: str = "0x0000000000000000000000000000000000000001",
    start_date: date = date(2024, 1, 1),
    end_date: date | None = None,
) -> Delegate:
    """Construct a Delegate with optional level assignments and field overrides.

    `levels` is typed `list[dict]` rather than `list[LevelAssignment]`
    because test callers feed in raw YAML-shaped dicts to exercise
    pydantic's coercion. Pydantic accepts that, but pyright treats
    `list` as invariant and won't accept `list[dict]` where
    `list[LevelAssignment]` is declared - hence the cast at the
    boundary.
    """
    return Delegate(
        name=name,
        vote_delegate_address=vote_delegate_address,
        start_date=start_date,
        end_date=end_date,
        levels=cast(list, levels) if levels is not None else [],
    )


def test_delegate_with_no_levels_constructs():
    """The common case: a delegate with no governance level assignment."""
    d = _delegate_with_levels(levels=[])
    assert d.levels == []
    assert d.level_at(date(2026, 1, 1)) is None


def test_delegate_with_levels_omitted_constructs():
    """Levels field is optional — omitting it is the same as empty list."""
    d = Delegate(
        name="X",
        vote_delegate_address="0x0000000000000000000000000000000000000002",
        start_date=date(2024, 1, 1),
    )
    assert d.levels == []


def test_level_must_be_1_or_2():
    """Level 3 in YAML is rejected — it's daily-computed, never YAML-set."""
    with pytest.raises(ValidationError, match="level must be 1 or 2"):
        _delegate_with_levels(levels=[{"level": 3, "start_date": date(2025, 12, 1)}])


def test_level_zero_rejected():
    with pytest.raises(ValidationError, match="level must be 1 or 2"):
        _delegate_with_levels(levels=[{"level": 0, "start_date": date(2025, 12, 1)}])


def test_level_negative_rejected():
    with pytest.raises(ValidationError, match="level must be 1 or 2"):
        _delegate_with_levels(levels=[{"level": -1, "start_date": date(2025, 12, 1)}])


def test_level_assignment_end_must_be_after_start():
    with pytest.raises(ValidationError, match="must be after"):
        _delegate_with_levels(
            levels=[
                {
                    "level": 1,
                    "start_date": date(2025, 12, 1),
                    "end_date": date(2025, 11, 1),
                }
            ]
        )


def test_level_period_must_fit_within_alignment_start():
    """LevelAssignment can't predate the delegate's alignment start_date."""
    with pytest.raises(ValidationError, match="before alignment start_date"):
        _delegate_with_levels(
            start_date=date(2024, 1, 1),
            levels=[{"level": 1, "start_date": date(2023, 6, 1)}],
        )


def test_level_period_must_fit_within_alignment_end():
    """LevelAssignment can't extend past the delegate's alignment end_date."""
    with pytest.raises(ValidationError, match="after alignment end_date"):
        _delegate_with_levels(
            start_date=date(2024, 1, 1),
            end_date=date(2025, 6, 30),
            levels=[
                {
                    "level": 1,
                    "start_date": date(2025, 1, 1),
                    "end_date": date(2025, 12, 31),
                }
            ],
        )


def test_open_level_with_exited_delegate_rejected():
    """A delegate who exited can't have an open-ended level — set both."""
    with pytest.raises(ValidationError, match="end_date"):
        _delegate_with_levels(
            start_date=date(2024, 1, 1),
            end_date=date(2025, 6, 30),
            levels=[{"level": 1, "start_date": date(2025, 1, 1), "end_date": None}],
        )


def test_overlapping_levels_rejected():
    """Two LevelAssignments for the same delegate may not overlap."""
    with pytest.raises(ValidationError, match="overlap"):
        _delegate_with_levels(
            levels=[
                {
                    "level": 2,
                    "start_date": date(2024, 6, 1),
                    "end_date": date(2025, 6, 30),
                },
                {
                    "level": 1,
                    "start_date": date(2025, 1, 1),  # overlaps with above
                    "end_date": date(2025, 12, 31),
                },
            ]
        )


def test_levels_with_open_ended_earlier_period_rejected():
    """An earlier LevelAssignment with no end_date can't be followed by another."""
    with pytest.raises(ValidationError, match="no end_date"):
        _delegate_with_levels(
            levels=[
                {"level": 2, "start_date": date(2024, 6, 1), "end_date": None},
                {"level": 1, "start_date": date(2025, 6, 1), "end_date": None},
            ]
        )


def test_sequential_levels_accepted():
    """L2 then L1 (or vice versa) with non-overlapping periods is allowed."""
    d = _delegate_with_levels(
        start_date=date(2024, 1, 1),
        levels=[
            {
                "level": 2,
                "start_date": date(2024, 6, 1),
                "end_date": date(2025, 5, 31),
            },
            {
                "level": 1,
                "start_date": date(2025, 6, 1),
                "end_date": None,
            },
        ],
    )
    assert len(d.levels) == 2


def test_adjacent_levels_with_one_day_gap_accepted():
    """Two LevelAssignments separated by even a single day apart don't overlap."""
    d = _delegate_with_levels(
        levels=[
            {
                "level": 2,
                "start_date": date(2024, 6, 1),
                "end_date": date(2025, 5, 31),
            },
            {
                "level": 1,
                "start_date": date(2025, 6, 1),
                "end_date": None,
            },
        ]
    )
    assert d.level_at(date(2025, 5, 31)) == 2
    assert d.level_at(date(2025, 6, 1)) == 1


# ---------------------------------------------------------------------------
# Delegate.level_at — daily lookup for L3 eligibility computation
# ---------------------------------------------------------------------------


def test_level_at_returns_none_for_unassigned_delegate():
    d = _delegate_with_levels(levels=[])
    assert d.level_at(date(2026, 1, 15)) is None


def test_level_at_returns_level_within_period():
    d = _delegate_with_levels(
        levels=[{"level": 1, "start_date": date(2025, 12, 1), "end_date": None}]
    )
    assert d.level_at(date(2026, 1, 15)) == 1


def test_level_at_returns_none_before_period_starts():
    d = _delegate_with_levels(
        levels=[{"level": 1, "start_date": date(2025, 12, 1), "end_date": None}]
    )
    assert d.level_at(date(2025, 11, 30)) is None


def test_level_at_returns_level_on_start_date_inclusive():
    d = _delegate_with_levels(
        levels=[{"level": 1, "start_date": date(2025, 12, 1), "end_date": None}]
    )
    assert d.level_at(date(2025, 12, 1)) == 1


def test_level_at_returns_level_on_end_date_inclusive():
    """end_date is inclusive: the delegate has the level on that day."""
    d = _delegate_with_levels(
        levels=[
            {
                "level": 1,
                "start_date": date(2025, 12, 1),
                "end_date": date(2026, 3, 31),
            }
        ]
    )
    assert d.level_at(date(2026, 3, 31)) == 1
    assert d.level_at(date(2026, 4, 1)) is None


def test_level_at_with_sequential_levels():
    """Across a level transition, returns the correct level for each date."""
    d = _delegate_with_levels(
        levels=[
            {
                "level": 2,
                "start_date": date(2024, 6, 1),
                "end_date": date(2025, 5, 31),
            },
            {
                "level": 1,
                "start_date": date(2025, 6, 1),
                "end_date": None,
            },
        ]
    )
    assert d.level_at(date(2024, 12, 1)) == 2
    assert d.level_at(date(2025, 5, 31)) == 2
    assert d.level_at(date(2025, 6, 1)) == 1
    assert d.level_at(date(2026, 1, 1)) == 1
    # Before any level period
    assert d.level_at(date(2024, 1, 1)) is None


# ---------------------------------------------------------------------------
# is_active_during — interval overlap with the queried month
# ---------------------------------------------------------------------------


def _delegate(start: date, end: date | None = None) -> Delegate:
    return Delegate(
        name="Harry",
        vote_delegate_address="0x1234567890abcdef1234567890abcdef12345678",
        start_date=start,
        end_date=end,
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
    # end_date is inclusive, so should be active
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
    with pytest.raises(ValidationError, match="Duplicate vote_delegate_address"):
        DelegatesConfig(
            delegates=[
                Delegate(name="A", vote_delegate_address=addr, start_date=date(2025, 1, 1)),
                Delegate(name="B", vote_delegate_address=addr, start_date=date(2025, 2, 1)),
            ]
        )


# ---------------------------------------------------------------------------
# load_delegates — file IO
# ---------------------------------------------------------------------------


def test_load_delegates_happy_path(tmp_path):
    yaml_text = """
    delegates:
      - name: Alice
        vote_delegate_address: "0x1234567890abcdef1234567890abcdef12348899"
        start_date: 2025-01-01
        end_date: null
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
        vote_delegate_address: "0x1234567890abcdef1234567890abcdef12348899"
        start_date: 2025-01-01
        end_date: null
      - name: Bob
        vote_delegate_address: "0xabcdef1234567890abcdef1234567890abcdef12"
        start_date: 2024-01-01
        end_date: 2024-06-30
        """
    p = tmp_path / "delegates.yaml"
    p.write_text(yaml_text)
    config = load_delegates(p)
    assert len(config.delegates) == 2
    assert config.delegates[1].end_date == date(2024, 6, 30)


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
      vote_delegate_address: "not-an-address"
      start_date: 2024-01-01
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
        assert d.vote_delegate_address.startswith("0x")
        assert len(d.vote_delegate_address) == 42


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
            Delegate(name="Active", vote_delegate_address=addr, start_date=date(2024, 1, 1)),
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
            Delegate(name="GhostlyActive", vote_delegate_address=addr, start_date=date(2024, 1, 1)),
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
                vote_delegate_address=addr,
                start_date=date(2024, 1, 1),
                end_date=date(2025, 6, 30),
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
                vote_delegate_address=addr,
                start_date=date(2024, 1, 1),
                end_date=date(2025, 6, 30),
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
            Delegate(name="X", vote_delegate_address=addr_lower, start_date=date(2024, 1, 1)),
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
            Delegate(name="BONAPUBLICA", vote_delegate_address=addr, start_date=date(2024, 1, 1)),
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
                name="OnlyInYaml", vote_delegate_address=addr_in_yaml, start_date=date(2024, 1, 1)
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
        vote_delegate_address: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        start_date: 2024-01-01
        end_date: null
      - name: ExitedBefore
        vote_delegate_address: "0x0f23de72e1581857eacd6308aebb69cf3a49cc86"
        start_date: 2023-01-01
        end_date: 2025-12-31
      - name: AlignedAfter
        vote_delegate_address: "0x173a1c04b79ed9266721c1154daa29addc0b9558"
        start_date: 2027-01-01
        end_date: null
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
        vote_delegate_address: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        start_date: 2024-01-01
        end_date: null
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
        vote_delegate_address: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        start_date: 2024-01-01
        end_date: null
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
        vote_delegate_address: "0xfc48fbca739079aab08216c4d5e506b96593753d"
        start_date: 2024-01-01
        end_date: null
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
            vote_delegate_address="0x0f23de72e1581857eacd6308aebb69cf3a49cc86",
            start_date=date(2023, 6, 6),
        ),
    ]
    df = to_dataframe(delegates)
    assert list(df.columns) == ["Delegate Name", "Delegate Contract", "Start Date"]


def test_to_dataframe_start_date_is_string():
    """sky_dao parses Start Date with strptime, so it must be a string in '%Y-%m-%d'."""
    delegates = [
        Delegate(
            name="X",
            vote_delegate_address="0xfc48fbca739079aab08216c4d5e506b96593753d",
            start_date=date(2024, 7, 4),
        ),
    ]
    df = to_dataframe(delegates)
    assert df.iloc[0]["Start Date"] == "2024-07-04"


def test_to_dataframe_preserves_address_format():
    delegates = [
        Delegate(
            name="X",
            vote_delegate_address="0xfc48fbca739079aab08216c4d5e506b96593753d",
            start_date=date(2024, 1, 1),
        ),
    ]
    df = to_dataframe(delegates)
    assert df.iloc[0]["Delegate Contract"] == "0xfc48fbca739079aab08216c4d5e506b96593753d"


def test_to_dataframe_empty():
    df = to_dataframe([])
    assert list(df.columns) == ["Delegate Name", "Delegate Contract", "Start Date"]
    assert len(df) == 0
