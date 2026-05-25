"""Daily eligibility and L1/L2/L3 slot assignment.

Pure function. Given the active roster, daily ranks, and per-delegate metric history, returns each active delegate's
participation %, communication %, eligibility flag, and assigned level for one day.

Slot model:
  - L1/L2 come from the YAML roster (`Delegate.levels`).
  - L3 slots = TOTAL_SLOTS - active_L1 - active_L2_, recomputed daily. A mid-period promotion shrinks L3 capacity,
    dropping the lowest-ranked L3 candidate.
  - L3 slots go to eligible delegates with the best ranks. Ties that cross the slot cutoff raise ValueError.

A delegate is eligible if participation_pct >= 0.75 AND communication_pct >= 0.75 over the trailing 6-month window. A
new delegate with no votable polls in the window is not eligible.

L1/L2 keep their slot regardless of metrics - the YAML is the source of truth. Metrics are still computed for them; the
compensation step uses the recorded percentages to apply modifiers.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from .metrics import communication_pct_for_window, participation_pct_for_window
from .roster import Delegate

TOTAL_SLOTS = 6
ELIGIBILITY_THRESHOLD = 0.75


@dataclass(frozen=True)
class DelegateMetricsInput:
    """Per-delegate poll/spell history feeding the eligibility computation.

    The three sequences are parallel - one entry per poll or spell. The eligibility computation drops anything outside
    the window.
    """

    poll_starts: Sequence[date]
    participation_statuses: Sequence[str]
    communication_statuses: Sequence[str]


@dataclass(frozen=True)
class DelegateEligibility:
    """Per-delegate result for one day.

    participation_pct and communication_pct are None when no polls in the window are in the votable (Yes/No) bucket;
    None -> eligible = False.
    assigned_level: 1 or 2 from the YAML, 3 from the daily L3 assignment, or None for unassigned L3 candidates.
    """

    rank: int
    participation_pct: float | None
    communication_pct: float | None
    eligible: bool
    assigned_level: int | None


@dataclass(frozen=True)
class DailyEligibility:
    """Full eligibility outcome for one day."""

    day: date
    l3_slots_available: int
    per_delegate: dict[str, DelegateEligibility]


@dataclass(frozen=True)
class _PartialResult:
    """Intermediate per-delegate state, before L3 slot assignment is decided."""

    rank: int
    participation_pct: float | None
    communication_pct: float | None
    eligible: bool
    yaml_level: int | None


def _validate_inputs_present(
    day: date,
    active_names: list[str],
    daily_ranks: Mapping[str, int],
    metrics_input: Mapping[str, DelegateMetricsInput],
) -> None:
    """Raise ValueError if any active delegate lacks a rank or metrics entry.

    Raises:
        ValueError: when any active delegate has no rank or no metrics entry.
    """
    missing_ranks = [n for n in active_names if n not in daily_ranks]
    if missing_ranks:
        msg = f"daily_ranks is missing for active delegates on {day}: {missing_ranks}"
        raise ValueError(msg)
    missing_metrics = [n for n in active_names if n not in metrics_input]
    if missing_metrics:
        msg = f"metrics_input is missing entries for active delegates on {day}: {missing_metrics}"
        raise ValueError(msg)


def _compute_partial_results(
    day: date,
    window: tuple[date, date],
    active_delegates: Sequence[Delegate],
    daily_ranks: Mapping[str, int],
    metrics_input: Mapping[str, DelegateMetricsInput],
) -> dict[str, _PartialResult]:
    """Compute metrics + provisional yaml_level for each active delegate.

    Returns:
        Mapping of delegate name to _PartialResult.
    """
    window_start, window_end = window
    per_delegate_partial: dict[str, _PartialResult] = {}
    for delegate in active_delegates:
        m = metrics_input[delegate.name]
        p = participation_pct_for_window(
            poll_starts=m.poll_starts,
            statuses=m.participation_statuses,
            window_start=window_start,
            window_end=window_end,
        )
        c = communication_pct_for_window(
            poll_starts=m.poll_starts,
            participation_statuses=m.participation_statuses,
            communication_statuses=m.communication_statuses,
            window_start=window_start,
            window_end=window_end,
        )
        eligible = p is not None and p >= ELIGIBILITY_THRESHOLD and c is not None and c >= ELIGIBILITY_THRESHOLD
        per_delegate_partial[delegate.name] = _PartialResult(
            rank=daily_ranks[delegate.name],
            participation_pct=p,
            communication_pct=c,
            eligible=eligible,
            yaml_level=delegate.level_at(day),
        )
    return per_delegate_partial


def _assign_l3_slots(
    day: date,
    per_delegate_partial: Mapping[str, _PartialResult],
) -> tuple[int, set[str]]:
    """Determine L3 slot capacity and which candidates get slots.

    Returns:
        (l3_slots_available, set of delegate names assigned an L3 slot).

    Raises:
        ValueError: if a rank tie spans the L3 slot cutoff.
    """
    l1_count = sum(1 for r in per_delegate_partial.values() if r.yaml_level == 1)
    l2_count = sum(1 for r in per_delegate_partial.values() if r.yaml_level == 2)  # noqa: PLR2004
    l3_slots_available = max(TOTAL_SLOTS - l1_count - l2_count, 0)

    l3_candidates = sorted(
        ((name, r) for name, r in per_delegate_partial.items() if r.yaml_level is None and r.eligible),
        key=lambda item: item[1].rank,
    )

    if 0 < l3_slots_available < len(l3_candidates):
        last_in_rank = l3_candidates[l3_slots_available - 1][1].rank
        first_out_rank = l3_candidates[l3_slots_available][1].rank
        if last_in_rank == first_out_rank:
            tied = [name for name, r in l3_candidates if r.rank == last_in_rank]
            msg = (
                f"L3 slot cutoff tie on {day}: rank {last_in_rank} is "
                f"shared by {len(tied)} delegates ({tied}) competing for "
                f"the last L3 slot. Resolve manually before finalizing."
            )
            raise ValueError(msg)

    return l3_slots_available, {name for name, _ in l3_candidates[:l3_slots_available]}


def compute_daily_eligibility(
    *,
    day: date,
    window: tuple[date, date],
    delegates: Sequence[Delegate],
    daily_ranks: Mapping[str, int],
    metrics_input: Mapping[str, DelegateMetricsInput],
) -> DailyEligibility:
    """Compute eligibility and slot assignment for every active delegate on `day`.

    `window` is (start, end) bounding the trailing metric window. For a finalize run the same window is usually passed for every day in the period - the 6-month metric is a track record, not a per-day rolling calculation.

    Active delegate are those where `is_active_during(day, day)` holds. Inactive delegates are silently excluded from
    the result, even if present in `daily_ranks` or `metrics_input` - callers may reuse one metrics dict across many
    days. Every active delegate must have both a rank and a metrics entry. ValueError propagates from validation
    (missing rank/metrics) or from L3 slot assignment (a rank tie spanning the slot cutoff).

    Returns:
        DailyEligibility with one entry per active delegate.
    """
    active_delegates = [d for d in delegates if d.is_active_during(day, day)]
    _validate_inputs_present(day, [d.name for d in active_delegates], daily_ranks, metrics_input)

    per_delegate_partial = _compute_partial_results(day, window, active_delegates, daily_ranks, metrics_input)
    l3_slots_available, l3_assigned_names = _assign_l3_slots(day, per_delegate_partial)

    per_delegate: dict[str, DelegateEligibility] = {}
    for name, r in per_delegate_partial.items():
        if r.yaml_level is not None:
            assigned_level: int | None = r.yaml_level
        elif name in l3_assigned_names:
            assigned_level = 3
        else:
            assigned_level = None
        per_delegate[name] = DelegateEligibility(
            rank=r.rank,
            participation_pct=r.participation_pct,
            communication_pct=r.communication_pct,
            eligible=r.eligible,
            assigned_level=assigned_level,
        )

    return DailyEligibility(
        day=day,
        l3_slots_available=l3_slots_available,
        per_delegate=per_delegate,
    )
