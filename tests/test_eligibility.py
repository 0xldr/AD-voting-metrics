"""Tests for the eligibility module — daily slot assignment and metric gating."""

import hashlib
from datetime import date

import pytest

from ad_voting_metrics.eligibility import (
    ELIGIBILITY_THRESHOLD,
    TOTAL_SLOTS,
    DelegateMetricsInput,
    compute_daily_eligibility,
)
from ad_voting_metrics.roster import Delegate, LevelAssignment

# ---------------------------------------------------------------------------
# Pinned constants
# ---------------------------------------------------------------------------


def test_total_slots_constant():
    assert TOTAL_SLOTS == 6


def test_eligibility_threshold_constant():
    assert ELIGIBILITY_THRESHOLD == 0.75


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Period under test: April 2026. Window: November 2025 through April 2026.
DAY = date(2026, 4, 15)
WINDOW_START = date(2025, 11, 1)
WINDOW_END = date(2026, 4, 30)


def _delegate(
    name: str,
    *,
    start_date: date = date(2024, 1, 1),
    end_date: date | None = None,
    levels: list[LevelAssignment] | None = None,
) -> Delegate:
    """Build a Delegate with sensible defaults.

    The vote_delegate_address is derived from a SHA-1 of the name so
    it's deterministic across runs and always matches the
    `^0x[0-9a-f]{40}$` pattern.
    """
    address_hex = hashlib.sha1(name.encode()).hexdigest()
    return Delegate(
        name=name,
        vote_delegate_address=f"0x{address_hex}",
        start_date=start_date,
        end_date=end_date,
        levels=levels or [],
    )


def _metrics(
    *,
    yeses: int = 0,
    nos: int = 0,
    comm_yeses: int | None = None,
    comm_nos: int | None = None,
) -> DelegateMetricsInput:
    """Build a DelegateMetricsInput with N Yes + M No participation entries.

    All poll starts are placed inside the window so they all count. If
    comm_* is omitted, communication mirrors participation perfectly
    (all in-window participated polls also communicated).
    """
    poll_starts: list[date] = []
    p_statuses: list[str] = []
    for _ in range(yeses):
        poll_starts.append(date(2026, 2, 1))
        p_statuses.append("Yes")
    for _ in range(nos):
        poll_starts.append(date(2026, 2, 1))
        p_statuses.append("No")

    if comm_yeses is None and comm_nos is None:
        # Perfect communication: 1 Yes per participated Yes; everything
        # else mirrored to "Did not vote" by the cross-reference.
        c_statuses = ["Yes" if p == "Yes" else "" for p in p_statuses]
    else:
        c_yeses = comm_yeses or 0
        c_nos = comm_nos or 0
        c_statuses = ["Yes"] * c_yeses + ["No"] * c_nos
        # Pad to match length so the parallel sequences line up.
        while len(c_statuses) < len(p_statuses):
            c_statuses.append("")
        c_statuses = c_statuses[: len(p_statuses)]

    return DelegateMetricsInput(
        poll_starts=poll_starts,
        participation_statuses=p_statuses,
        communication_statuses=c_statuses,
    )


def _no_polls_metrics() -> DelegateMetricsInput:
    return DelegateMetricsInput(
        poll_starts=[],
        participation_statuses=[],
        communication_statuses=[],
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_missing_rank_for_active_delegate_raises():
    """Every active delegate must have a rank entry."""
    delegates = [_delegate("Alpha"), _delegate("Beta")]
    with pytest.raises(ValueError, match="daily_ranks is missing"):
        compute_daily_eligibility(
            day=DAY,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            delegates=delegates,
            daily_ranks={"Alpha": 1},  # Beta missing
            metrics_input={"Alpha": _metrics(yeses=10), "Beta": _metrics(yeses=10)},
        )


def test_missing_metrics_for_active_delegate_raises():
    delegates = [_delegate("Alpha"), _delegate("Beta")]
    with pytest.raises(ValueError, match="metrics_input is missing"):
        compute_daily_eligibility(
            day=DAY,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            delegates=delegates,
            daily_ranks={"Alpha": 1, "Beta": 2},
            metrics_input={"Alpha": _metrics(yeses=10)},  # Beta missing
        )


def test_inactive_delegate_silently_excluded():
    """A delegate inactive on the queried day doesn't need rank/metrics."""
    delegates = [
        _delegate("Active"),
        _delegate("Old", end_date=date(2025, 12, 31)),  # exited before April 2026
    ]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Active": 1},  # Old absent, fine
        metrics_input={"Active": _metrics(yeses=10)},  # Old absent, fine
    )
    assert "Active" in result.per_delegate
    assert "Old" not in result.per_delegate


# ---------------------------------------------------------------------------
# Per-delegate metrics
# ---------------------------------------------------------------------------


def test_perfect_record_eligible():
    delegates = [_delegate("Alpha")]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Alpha": 1},
        metrics_input={"Alpha": _metrics(yeses=10)},
    )
    alpha = result.per_delegate["Alpha"]
    assert alpha.participation_pct == 1.0
    assert alpha.communication_pct == 1.0
    assert alpha.eligible is True
    assert alpha.assigned_level == 3


def test_below_participation_threshold_ineligible():
    """7 Yes + 3 No = 70% participation, below 75%."""
    delegates = [_delegate("Alpha")]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Alpha": 1},
        metrics_input={"Alpha": _metrics(yeses=7, nos=3)},
    )
    alpha = result.per_delegate["Alpha"]
    assert alpha.participation_pct == 0.7
    assert alpha.eligible is False
    assert alpha.assigned_level is None


def test_below_communication_threshold_ineligible():
    """Participation perfect, communication 70% (7 Yes, 3 No in comm)."""
    delegates = [_delegate("Alpha")]
    metrics = DelegateMetricsInput(
        poll_starts=[date(2026, 2, 1)] * 10,
        participation_statuses=["Yes"] * 10,
        communication_statuses=["Yes"] * 7 + ["No"] * 3,
    )
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Alpha": 1},
        metrics_input={"Alpha": metrics},
    )
    alpha = result.per_delegate["Alpha"]
    assert alpha.participation_pct == 1.0
    assert alpha.communication_pct == 0.7
    assert alpha.eligible is False


def test_exactly_at_threshold_eligible():
    """75% is eligible — boundary is inclusive."""
    delegates = [_delegate("Alpha")]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Alpha": 1},
        metrics_input={"Alpha": _metrics(yeses=3, nos=1)},  # 75%
    )
    alpha = result.per_delegate["Alpha"]
    assert alpha.participation_pct == 0.75
    assert alpha.eligible is True


def test_no_votable_polls_returns_none_pct_and_ineligible():
    """New delegate with no closed polls in window → not eligible."""
    delegates = [_delegate("Newbie", start_date=date(2026, 4, 14))]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Newbie": 1},
        metrics_input={"Newbie": _no_polls_metrics()},
    )
    newbie = result.per_delegate["Newbie"]
    assert newbie.participation_pct is None
    assert newbie.communication_pct is None
    assert newbie.eligible is False
    assert newbie.assigned_level is None


def test_polls_outside_window_ignored():
    """A delegate with all polls outside the window has no votable polls in window."""
    delegates = [_delegate("Alpha")]
    metrics = DelegateMetricsInput(
        # All polls in 2024 — far before the Nov 2025 window start
        poll_starts=[date(2024, 6, 1)] * 10,
        participation_statuses=["Yes"] * 10,
        communication_statuses=["Yes"] * 10,
    )
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"Alpha": 1},
        metrics_input={"Alpha": metrics},
    )
    alpha = result.per_delegate["Alpha"]
    assert alpha.participation_pct is None
    assert alpha.eligible is False


# ---------------------------------------------------------------------------
# L1 / L2 from YAML
# ---------------------------------------------------------------------------


def test_l1_assignment_from_yaml_overrides_rank():
    """L1 delegates get assigned_level=1 even at rank 99."""
    delegates = [
        _delegate(
            "L1Person",
            levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))],
        ),
    ]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"L1Person": 99},
        metrics_input={"L1Person": _metrics(yeses=10)},
    )
    assert result.per_delegate["L1Person"].assigned_level == 1


def test_l2_assignment_from_yaml():
    delegates = [
        _delegate(
            "L2Person",
            levels=[LevelAssignment(level=2, start_date=date(2024, 1, 1))],
        ),
    ]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"L2Person": 5},
        metrics_input={"L2Person": _metrics(yeses=10)},
    )
    assert result.per_delegate["L2Person"].assigned_level == 2


def test_l1_below_threshold_keeps_slot_but_records_ineligible():
    """L1/L2 keep their slot regardless of metrics; comp step handles modifiers."""
    delegates = [
        _delegate(
            "BadL1",
            levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))],
        ),
    ]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"BadL1": 1},
        metrics_input={"BadL1": _metrics(yeses=5, nos=5)},  # 50%
    )
    bad = result.per_delegate["BadL1"]
    assert bad.assigned_level == 1  # keeps slot
    assert bad.eligible is False  # recorded as ineligible


def test_l1_assignment_inactive_on_day_excluded():
    """A LevelAssignment that ended yesterday → no L1/L2 today, just L3 candidate."""
    delegates = [
        _delegate(
            "FormerL1",
            levels=[
                LevelAssignment(
                    level=1,
                    start_date=date(2024, 1, 1),
                    end_date=date(2026, 4, 14),  # ended yesterday
                ),
            ],
        ),
    ]
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks={"FormerL1": 1},
        metrics_input={"FormerL1": _metrics(yeses=10)},
    )
    # No longer L1; rank-1 eligible candidate → L3 slot.
    assert result.per_delegate["FormerL1"].assigned_level == 3


# ---------------------------------------------------------------------------
# L3 slot capacity
# ---------------------------------------------------------------------------


def test_zero_l1_l2_yields_six_l3_slots():
    delegates = [_delegate(f"D{i}") for i in range(10)]
    ranks = {f"D{i}": i + 1 for i in range(10)}
    metrics = {f"D{i}": _metrics(yeses=10) for i in range(10)}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.l3_slots_available == 6


def test_two_l1_and_one_l2_yields_three_l3_slots():
    delegates = [
        _delegate("L1A", levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))]),
        _delegate("L1B", levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))]),
        _delegate("L2A", levels=[LevelAssignment(level=2, start_date=date(2024, 1, 1))]),
        _delegate("Cand1"),
        _delegate("Cand2"),
        _delegate("Cand3"),
        _delegate("Cand4"),
    ]
    ranks = {d.name: i + 1 for i, d in enumerate(delegates)}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.l3_slots_available == 3
    # Top 3 candidates by rank get L3.
    assert result.per_delegate["Cand1"].assigned_level == 3
    assert result.per_delegate["Cand2"].assigned_level == 3
    assert result.per_delegate["Cand3"].assigned_level == 3
    assert result.per_delegate["Cand4"].assigned_level is None


def test_six_l1_yields_zero_l3_slots():
    delegates = [
        _delegate(f"L1{i}", levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))])
        for i in range(6)
    ]
    delegates.append(_delegate("Cand"))
    ranks = {d.name: i + 1 for i, d in enumerate(delegates)}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.l3_slots_available == 0
    assert result.per_delegate["Cand"].assigned_level is None


def test_over_assigned_governance_yields_zero_l3_slots_no_error():
    """Operator over-assigns L1/L2 → l3_slots clamps to 0, no error."""
    delegates = [
        _delegate(f"L1{i}", levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))])
        for i in range(7)
    ]
    ranks = {d.name: i + 1 for i, d in enumerate(delegates)}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.l3_slots_available == 0


# ---------------------------------------------------------------------------
# L3 slot assignment
# ---------------------------------------------------------------------------


def test_fewer_eligible_candidates_than_slots_all_get_l3():
    """3 eligible candidates, 6 slots → all 3 get L3, no error."""
    delegates = [_delegate(f"C{i}") for i in range(3)]
    ranks = {d.name: i + 1 for i, d in enumerate(delegates)}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    for d in delegates:
        assert result.per_delegate[d.name].assigned_level == 3


def test_ineligible_candidate_skipped_for_l3_slot():
    """Rank-1 ineligible candidate doesn't get L3; rank-2 eligible does."""
    delegates = [_delegate("Bad"), _delegate("Good")]
    ranks = {"Bad": 1, "Good": 2}
    metrics = {
        "Bad": _metrics(yeses=5, nos=5),  # 50%, ineligible
        "Good": _metrics(yeses=10),  # 100%, eligible
    }
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.per_delegate["Bad"].assigned_level is None
    assert result.per_delegate["Good"].assigned_level == 3


def test_tie_within_granted_set_no_error():
    """Two delegates tied at rank 1 + 4 more at higher ranks, 6 slots → all in, no error."""
    delegates = [_delegate(f"C{i}") for i in range(6)]
    ranks = {"C0": 1, "C1": 1, "C2": 3, "C3": 4, "C4": 5, "C5": 6}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    for d in delegates:
        assert result.per_delegate[d.name].assigned_level == 3


def test_tie_crossing_cutoff_raises():
    """7 candidates, 6 slots, ranks 1-5 unique then 6,6 → tie at cutoff → raise."""
    delegates = [_delegate(f"C{i}") for i in range(7)]
    ranks = {"C0": 1, "C1": 2, "C2": 3, "C3": 4, "C4": 5, "C5": 6, "C6": 6}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    with pytest.raises(ValueError, match="cutoff tie"):
        compute_daily_eligibility(
            day=DAY,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
            delegates=delegates,
            daily_ranks=ranks,
            metrics_input=metrics,
        )


def test_tie_excluded_from_consideration_no_error():
    """Tie at rank 7+ (well outside cutoff) → no error, both excluded."""
    delegates = [_delegate(f"C{i}") for i in range(8)]
    ranks = {"C0": 1, "C1": 2, "C2": 3, "C3": 4, "C4": 5, "C5": 6, "C6": 7, "C7": 7}
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    # First 6 get L3, last 2 (both rank 7) don't.
    for i in range(6):
        assert result.per_delegate[f"C{i}"].assigned_level == 3
    assert result.per_delegate["C6"].assigned_level is None
    assert result.per_delegate["C7"].assigned_level is None


def test_zero_l3_slots_no_tie_check_runs():
    """When 0 L3 slots, candidate ties don't matter — no error."""
    delegates = [
        _delegate(f"L1{i}", levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))])
        for i in range(6)
    ]
    delegates.extend([_delegate("CandA"), _delegate("CandB")])
    ranks = {d.name: i + 1 for i, d in enumerate(delegates)}
    ranks["CandA"] = 7
    ranks["CandB"] = 7  # tie, but doesn't matter
    metrics = {d.name: _metrics(yeses=10) for d in delegates}
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )
    assert result.l3_slots_available == 0


# ---------------------------------------------------------------------------
# Realistic mix
# ---------------------------------------------------------------------------


def test_realistic_april_2026_scenario():
    """Mix of L1, L2, eligible/ineligible L3 candidates, and a new delegate."""
    delegates = [
        _delegate(
            "Aligned1",
            levels=[LevelAssignment(level=1, start_date=date(2024, 1, 1))],
        ),
        _delegate(
            "Aligned2",
            levels=[LevelAssignment(level=2, start_date=date(2024, 1, 1))],
        ),
        _delegate("Top"),  # eligible, rank 3 → L3
        _delegate("Mid"),  # eligible, rank 4 → L3
        _delegate("Below"),  # eligible, rank 5 → L3
        _delegate("Marginal"),  # eligible, rank 6 → L3
        _delegate("Skipped"),  # eligible, rank 7 → no slot (only 4 L3 slots)
        _delegate("Failing"),  # ineligible (50%), rank 2 — high rank but skipped
        _delegate("Newbie", start_date=date(2026, 4, 14)),  # no data, ineligible
    ]
    ranks = {
        "Aligned1": 99,  # rank irrelevant for L1
        "Aligned2": 50,  # rank irrelevant for L2
        "Failing": 2,
        "Top": 3,
        "Mid": 4,
        "Below": 5,
        "Marginal": 6,
        "Skipped": 7,
        "Newbie": 1,
    }
    metrics = {
        "Aligned1": _metrics(yeses=10),
        "Aligned2": _metrics(yeses=10),
        "Top": _metrics(yeses=10),
        "Mid": _metrics(yeses=10),
        "Below": _metrics(yeses=10),
        "Marginal": _metrics(yeses=10),
        "Skipped": _metrics(yeses=10),
        "Failing": _metrics(yeses=5, nos=5),
        "Newbie": _no_polls_metrics(),
    }
    result = compute_daily_eligibility(
        day=DAY,
        window_start=WINDOW_START,
        window_end=WINDOW_END,
        delegates=delegates,
        daily_ranks=ranks,
        metrics_input=metrics,
    )

    # 1 L1 + 1 L2 = 2 governance → 4 L3 slots.
    assert result.l3_slots_available == 4

    assert result.per_delegate["Aligned1"].assigned_level == 1
    assert result.per_delegate["Aligned2"].assigned_level == 2
    assert result.per_delegate["Top"].assigned_level == 3
    assert result.per_delegate["Mid"].assigned_level == 3
    assert result.per_delegate["Below"].assigned_level == 3
    assert result.per_delegate["Marginal"].assigned_level == 3
    assert result.per_delegate["Skipped"].assigned_level is None
    assert result.per_delegate["Failing"].assigned_level is None
    assert result.per_delegate["Newbie"].assigned_level is None

    # Per-delegate eligibility recorded correctly.
    assert result.per_delegate["Aligned1"].eligible is True
    assert result.per_delegate["Failing"].eligible is False
    assert result.per_delegate["Newbie"].eligible is False
    assert result.per_delegate["Newbie"].participation_pct is None
