"""Tests for the compensation module — pro-rata + metrics modifier."""

from datetime import date

import pytest

from ad_voting_metrics.compensation import (
    CompensationConfig,
    component_modifier,
    compute_period_compensation,
)
from ad_voting_metrics.eligibility import DailyEligibility, DelegateEligibility
from ad_voting_metrics.period import MonthPeriod

# ---------------------------------------------------------------------------
# component_modifier — single metric → ramp scalar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        (None, 0.0),  # missing data
        (0.74, 0.0),  # below floor
        (0.75, 0.0),  # exactly at floor (ramp value 0)
        (0.85, 0.5),  # mid-ramp (halfway between 0.75 and 0.95)
        (0.94, 0.95),  # just below ceiling: (0.94 - 0.75) / 0.20
        (0.95, 1.0),  # exactly at ceiling
        (1.0, 1.0),  # above ceiling
    ],
)
def test_component_modifier(pct, expected):
    assert component_modifier(pct) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


PERIOD = MonthPeriod(year=2026, month=4)
DAYS_IN_APRIL = 30
CONFIG = CompensationConfig(
    l1_usds=33333.0,
    l2_usds=14583.0,
    l3_usds=4000.0,
    total_slots=6,
)


def _delegate_eligibility(
    *,
    assigned_level: int | None,
    rank: int = 1,
    eligible: bool = True,
    participation_pct: float | None = 1.0,
    communication_pct: float | None = 1.0,
) -> DelegateEligibility:
    return DelegateEligibility(
        rank=rank,
        participation_pct=participation_pct,
        communication_pct=communication_pct,
        eligible=eligible,
        assigned_level=assigned_level,
    )


def _daily(day: date, per_delegate: dict[str, DelegateEligibility]) -> DailyEligibility:
    return DailyEligibility(
        day=day,
        l3_slots_available=6,
        per_delegate=per_delegate,
    )


def _full_period(per_delegate: dict[str, DelegateEligibility]) -> list[DailyEligibility]:
    """Return one DailyEligibility per day in April 2026, identical contents."""
    return [_daily(date(2026, 4, d), per_delegate) for d in range(1, DAYS_IN_APRIL + 1)]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_wrong_day_count_raises():
    """Period has 30 days; passing 29 DailyEligibility entries raises."""
    de = _delegate_eligibility(assigned_level=3)
    days = [_daily(date(2026, 4, d), {"Alice": de}) for d in range(1, 30)]
    with pytest.raises(ValueError, match=r"29 entries but period .* has 30 days"):
        compute_period_compensation(
            period=PERIOD,
            daily_eligibility=days,
            config=CONFIG,
            final_metrics={"Alice": (1.0, 1.0)},
        )


def test_missing_final_metrics_for_active_delegate_raises():
    de = _delegate_eligibility(assigned_level=3)
    with pytest.raises(ValueError, match="final_metrics is missing"):
        compute_period_compensation(
            period=PERIOD,
            daily_eligibility=_full_period({"Alice": de}),
            config=CONFIG,
            final_metrics={},
        )


# ---------------------------------------------------------------------------
# Per-delegate compensation
# ---------------------------------------------------------------------------


def test_full_period_l3_perfect_metrics():
    """L3 for all 30 days, 100% metrics → full L3 amount."""
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.name == "Alice"
    assert row.days_as_l3 == 30
    assert row.entitlement_pre_modifier == pytest.approx(4000.0)
    assert row.metrics_modifier == 1.0
    assert row.final_amount == 4000.0
    assert not row.notes


def test_full_period_l1_perfect_metrics():
    de = _delegate_eligibility(assigned_level=1)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.days_as_l1 == 30
    assert row.entitlement_pre_modifier == pytest.approx(33333.0)
    assert row.final_amount == 33333.0


def test_partial_period_l3():
    """15 days L3 out of 30 → half the L3 amount."""
    half_l3 = [
        _daily(date(2026, 4, d), {"Alice": _delegate_eligibility(assigned_level=3)}) for d in range(1, 16)
    ]
    half_none = [
        _daily(date(2026, 4, d), {"Alice": _delegate_eligibility(assigned_level=None)}) for d in range(16, 31)
    ]
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=half_l3 + half_none,
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.days_as_l3 == 15
    assert row.entitlement_pre_modifier == pytest.approx(2000.0)
    assert row.final_amount == 2000.0


def test_mid_period_promotion_l3_to_l1():
    """15 days L3 + 15 days L1 → (15/30)*4000 + (15/30)*33333 = 2000 + 16666.5."""
    days = [_daily(date(2026, 4, d), {"Alice": _delegate_eligibility(assigned_level=3)}) for d in range(1, 16)]
    days.extend(
        _daily(date(2026, 4, d), {"Alice": _delegate_eligibility(assigned_level=1)}) for d in range(16, 31)
    )
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=days,
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.days_as_l3 == 15
    assert row.days_as_l1 == 15
    expected = (15 / 30) * 4000.0 + (15 / 30) * 33333.0
    assert row.entitlement_pre_modifier == pytest.approx(expected)
    assert row.final_amount == round(expected, 0)


def test_modifier_applied_to_final():
    """L3 full period, both metrics at 0.85 → modifier 0.5 * 0.5 = 0.25."""
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (0.85, 0.85)},
    )
    row = result.per_delegate[0]
    assert row.metrics_modifier == pytest.approx(0.25)
    assert row.final_amount == 1000.0  # round(4000 * 0.25)


def test_modifier_below_floor_zeros_payment():
    """Metric at 0.70 → modifier 0.0 → final 0 even with full days."""
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (0.70, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.metrics_modifier == 0.0
    assert row.final_amount == 0.0


def test_modifier_none_metric_zeros_payment():
    """No votable polls (None) → modifier 0 → final 0."""
    de = _delegate_eligibility(
        assigned_level=3,
        participation_pct=None,
        communication_pct=None,
        eligible=False,
    )
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (None, None)},
    )
    row = result.per_delegate[0]
    assert row.metrics_modifier == 0.0
    assert row.final_amount == 0.0


def test_unassigned_delegate_gets_zero():
    """Delegate present every day but never assigned a slot → zero entitlement."""
    de = _delegate_eligibility(assigned_level=None)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.days_as_l1 == row.days_as_l2 == row.days_as_l3 == 0
    assert row.entitlement_pre_modifier == 0.0
    assert row.final_amount == 0.0


# ---------------------------------------------------------------------------
# Notes column
# ---------------------------------------------------------------------------


def test_notes_blank_when_modifier_is_one():
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    assert not result.per_delegate[0].notes


def test_notes_populated_when_modifier_under_one():
    """Notes carry the exact modifier percentage."""
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (0.85, 0.85)},
    )
    assert result.per_delegate[0].notes == "Payments reduced to 25.00% via metrics modifier"


def test_notes_blank_when_no_entitlement():
    """A delegate with zero days held gets no notes regardless of modifier."""
    de = _delegate_eligibility(
        assigned_level=None,
        participation_pct=0.85,
        communication_pct=0.85,
    )
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (0.85, 0.85)},
    )
    assert not result.per_delegate[0].notes


# ---------------------------------------------------------------------------
# Buffer stubs
# ---------------------------------------------------------------------------


def test_buffer_fields_are_stubs():
    """All buffer fields except buffer_added (=final_amount) are 0."""
    de = _delegate_eligibility(assigned_level=3)
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period({"Alice": de}),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.buffer_carry_in == 0.0
    assert row.buffer_added == row.final_amount
    assert row.payment_amount == 0.0
    assert row.buffer_post_payment == 0.0


# ---------------------------------------------------------------------------
# End-of-period rank and level
# ---------------------------------------------------------------------------


def test_rank_and_level_taken_from_last_day():
    """rank_at_period_end and level_at_period_end come from day 30."""
    days = [
        _daily(
            date(2026, 4, d),
            {"Alice": _delegate_eligibility(assigned_level=3, rank=1)},
        )
        for d in range(1, 30)
    ]
    days.append(
        _daily(
            date(2026, 4, 30),
            {"Alice": _delegate_eligibility(assigned_level=1, rank=2)},
        ),
    )
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=days,
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0)},
    )
    row = result.per_delegate[0]
    assert row.rank_at_period_end == 2
    assert row.level_at_period_end == 1


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


def test_per_delegate_sorted_alphabetically():
    """Output rows come back in alphabetical name order regardless of input."""
    delegates = {
        "Charlie": _delegate_eligibility(assigned_level=3),
        "Alice": _delegate_eligibility(assigned_level=3),
        "Bob": _delegate_eligibility(assigned_level=3),
    }
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period(delegates),
        config=CONFIG,
        final_metrics={"Alice": (1.0, 1.0), "Bob": (1.0, 1.0), "Charlie": (1.0, 1.0)},
    )
    assert [r.name for r in result.per_delegate] == ["Alice", "Bob", "Charlie"]


# ---------------------------------------------------------------------------
# Validation checks
# ---------------------------------------------------------------------------


def test_slot_days_check_good_when_full():
    """30 days x 6 slots = 180 slot-days, all filled → GOOD."""
    delegates = {f"D{i}": _delegate_eligibility(assigned_level=3, rank=i) for i in range(6)}
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period(delegates),
        config=CONFIG,
        final_metrics={f"D{i}": (1.0, 1.0) for i in range(6)},
    )
    assert result.validation["slot_days_check"] == "GOOD"


def test_slot_days_check_not_good_when_underfilled():
    """Only 3 slots filled out of 6 → 90 slot-days vs 180 expected → NOT GOOD."""
    delegates = {f"D{i}": _delegate_eligibility(assigned_level=3, rank=i) for i in range(3)}
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period(delegates),
        config=CONFIG,
        final_metrics={f"D{i}": (1.0, 1.0) for i in range(3)},
    )
    assert result.validation["slot_days_check"] == "NOT GOOD"


# ---------------------------------------------------------------------------
# Realistic scenario
# ---------------------------------------------------------------------------


def test_realistic_april_scenario():
    """1 L1 + 1 L2 + 4 L3 for the full month with varied metric modifiers."""
    delegates = {
        "Aligned1": _delegate_eligibility(assigned_level=1, rank=99),
        "Aligned2": _delegate_eligibility(assigned_level=2, rank=50),
        "Top": _delegate_eligibility(assigned_level=3, rank=3),
        "Mid": _delegate_eligibility(assigned_level=3, rank=4),
        "Below": _delegate_eligibility(assigned_level=3, rank=5),
        "Marginal": _delegate_eligibility(assigned_level=3, rank=6),
    }
    final_metrics = {
        "Aligned1": (1.0, 1.0),  # full pay
        "Aligned2": (0.85, 1.0),  # p modifier 0.5 → 0.5 final
        "Top": (1.0, 1.0),  # full
        "Mid": (0.95, 0.95),  # full (both at ceiling)
        "Below": (0.80, 1.0),  # p modifier 0.25 → 0.25 final
        "Marginal": (0.70, 1.0),  # below floor → 0
    }
    result = compute_period_compensation(
        period=PERIOD,
        daily_eligibility=_full_period(delegates),
        config=CONFIG,
        final_metrics=final_metrics,
    )

    rows_by_name = {r.name: r for r in result.per_delegate}
    assert rows_by_name["Aligned1"].final_amount == 33333.0
    assert rows_by_name["Aligned2"].final_amount == round(14583.0 * 0.5, 0)  # 7292
    assert rows_by_name["Top"].final_amount == 4000.0
    assert rows_by_name["Mid"].final_amount == 4000.0
    assert rows_by_name["Below"].final_amount == round(4000.0 * 0.25, 0)  # 1000
    assert rows_by_name["Marginal"].final_amount == 0.0
    assert rows_by_name["Marginal"].notes == "Payments reduced to 0.00% via metrics modifier"
