"""Period compensation: pro-rata by days held, scaled by the metrics modifier.

Pure functions. Given per-day eligibility outputs and period-end metric percentages, returns one DelegateCompensation
per active delegate, alphabetical by name.

Per delegate:
  entitlement = (days_as_l1 / N) * L1_USDS + (days_as_l2 / N) * L2_USDS + (days_as_l3 / N) * L3_USDS
  modifier    = component(p_pct) * component(c_pct)
  final       = round(entitlement * modifier, 0)

Component modifier (per the SKY DAO brief):
  >= 0.95  → 1.0
  >= 0.75  → (pct - 0.75) / 0.20   (linear ramp 0 → 1)
  < 0.75   → 0.0
  None     → 0.0

Buffer columns (carry-in, payment, post-payment) are stubs returning 0.0.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .eligibility import DailyEligibility
from .period import MonthPeriod

_BUDGET_FLOOR = 0.75
_BUDGET_CEILING = 0.95
_RAMP_WIDTH = _BUDGET_CEILING - _BUDGET_FLOOR


@dataclass(frozen=True)
class CompensationConfig:
    """L1/L2/L3 monthly USDS amounts and the total slot count.

    Read from the workbook's Config tab; all four values are required.
    """

    l1_usds: float
    l2_usds: float
    l3_usds: float
    total_slots: int


@dataclass(frozen=True)
class DelegateCompensation:
    """Per-delegate compensation row for the period.

    Days fields sum to days held at any level (≤ days_in_period). Buffer fields are stubs: carry_in/payment/post_payment
    are 0.0 pending the buffer carry-over implementation.
    """

    name: str
    rank_at_period_end: int | None
    level_at_period_end: int | None
    days_as_l1: int
    days_as_l2: int
    days_as_l3: int
    participation_pct: float | None
    communication_pct: float | None
    metrics_modifier: float
    entitlement_pre_modifier: float
    final_amount: float
    buffer_carry_in: float
    buffer_added: float
    payment_amount: float
    buffer_post_payment: float
    notes: str


@dataclass(frozen=True)
class PeriodCompensation:
    """Period-level compensation output.

    `per_delegate` is sorted alphabetically by name. `validation` records the workbook-equivalent GOOD/NOT GOOD checks.
    """

    period: MonthPeriod
    config: CompensationConfig
    days_in_period: int
    per_delegate: list[DelegateCompensation]
    validation: dict[str, str] = field(default_factory=dict)


def component_modifier(pct: float | None) -> float:
    """Map a single metric pct to its component modifier in [0.0, 1.0].

    Returns:
        0.0 for None or below 0.75, 1.0 for >= 0.95, and a linear ramp in between.
    """
    if pct is None or pct < _BUDGET_FLOOR:
        return 0.0
    if pct >= _BUDGET_CEILING:
        return 1.0
    return (pct - _BUDGET_FLOOR) / _RAMP_WIDTH


def _aggregate_daily_eligibility(
    daily_eligibility: Sequence[DailyEligibility],
) -> tuple[dict[str, dict[int, int]], dict[str, tuple[int | None, int | None]]]:
    """Collect per-delegate level-day counts and (rank, level) on the final day.

    Returns:
        Tuple of (day_counts, end_state). day_counts maps name to {1: count, 2: count, 3: count}. end_state maps name to
        (rank, assigned_level) from the period's last day.
    """
    day_counts: dict[str, dict[int, int]] = {}
    end_state: dict[str, tuple[int | None, int | None]] = {}
    last_day_index = len(daily_eligibility) - 1
    for i, de in enumerate(daily_eligibility):
        for name, entry in de.per_delegate.items():
            counts = day_counts.setdefault(name, {1: 0, 2: 0, 3: 0})
            if entry.assigned_level in {1, 2, 3}:
                counts[entry.assigned_level] += 1
            if i == last_day_index:
                end_state[name] = (entry.rank, entry.assigned_level)
    return day_counts, end_state


def _build_delegate_compensation(
    name: str,
    counts: dict[int, int],
    end_state_entry: tuple[int | None, int | None],
    metrics_entry: tuple[float | None, float | None],
    context: tuple[int, CompensationConfig],
) -> DelegateCompensation:
    """Build a single DelegateCompensation row from aggregated inputs.

    Returns:
        The DelegateCompensation row for `name`.
    """
    days_in_period, config = context
    d1, d2, d3 = counts[1], counts[2], counts[3]
    entitlement = (
        (d1 / days_in_period) * config.l1_usds
        + (d2 / days_in_period) * config.l2_usds
        + (d3 / days_in_period) * config.l3_usds
    )
    p_pct, c_pct = metrics_entry
    modifier = component_modifier(p_pct) * component_modifier(c_pct)
    final_amount = round(entitlement * modifier, 0)
    if modifier < 1.0 and entitlement > 0:
        notes = f"Payments reduced to {modifier * 100:.2f}% via metrics modifier"
    else:
        notes = ""
    rank, level = end_state_entry
    return DelegateCompensation(
        name=name,
        rank_at_period_end=rank,
        level_at_period_end=level,
        days_as_l1=d1,
        days_as_l2=d2,
        days_as_l3=d3,
        participation_pct=p_pct,
        communication_pct=c_pct,
        metrics_modifier=modifier,
        entitlement_pre_modifier=entitlement,
        final_amount=final_amount,
        buffer_carry_in=0.0,
        buffer_added=final_amount,
        payment_amount=0.0,
        buffer_post_payment=0.0,
        notes=notes,
    )


def compute_period_compensation(
    *,
    period: MonthPeriod,
    daily_eligibility: Sequence[DailyEligibility],
    config: CompensationConfig,
    final_metrics: Mapping[str, tuple[float | None, float | None]],
) -> PeriodCompensation:
    """Compute compensation for every delegate active at any point in the period.

    `daily_eligibility` must have one entry per day in the period. `final_metrics` maps delegate name →
    (participation_pct, communication_pct) evaluated on the period's last day (the 6-month track-record values that gate
    the modifier).

    The output is sorted alphabetically by delegate name.

    Returns:
        PeriodCompensation with alphabetical per-delegate rows.

    Raises:
        ValueError: if daily_eligibility's day count doesn't match the period's day count, or if a delegate appears in
        daily_eligibility but is missing from final_metrics.
    """
    days_in_period = (period.end - period.start).days + 1
    if len(daily_eligibility) != days_in_period:
        msg = f"daily_eligibility has {len(daily_eligibility)} entries but period {period} has {days_in_period} days"
        raise ValueError(msg)

    day_counts, end_state = _aggregate_daily_eligibility(daily_eligibility)

    missing_metrics = sorted(set(day_counts) - set(final_metrics))
    if missing_metrics:
        msg_0 = f"final_metrics is missing entries for active delegates: {missing_metrics}"
        raise ValueError(msg_0)

    context = (days_in_period, config)
    rows = [
        _build_delegate_compensation(
            name,
            day_counts[name],
            end_state.get(name, (None, None)),
            final_metrics[name],
            context,
        )
        for name in sorted(day_counts)
    ]

    total_slot_days = sum(r.days_as_l1 + r.days_as_l2 + r.days_as_l3 for r in rows)
    expected_slot_days = days_in_period * config.total_slots
    days_check = "GOOD" if total_slot_days == expected_slot_days else "NOT GOOD"

    return PeriodCompensation(
        period=period,
        config=config,
        days_in_period=days_in_period,
        per_delegate=rows,
        validation={"slot_days_check": days_check},
    )
