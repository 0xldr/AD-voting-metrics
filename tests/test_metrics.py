"""Tests for the metrics module - participation percentage and window filtering."""

from datetime import date

import pytest

from ad_voting_metrics.metrics import (
    DISCOUNTED,
    NOT_PARTICIPATED,
    PARTICIPATED,
    ParticipationCounts,
    count_statuses,
    is_in_window,
    participation_pct,
    participation_pct_for_window,
)

# ---------------------------------------------------------------------------
# Status set sanity — guard against accidental edits to the constants
# ---------------------------------------------------------------------------


def test_participated_set_contents():
    """Pin the participated set so an accidental edit fails the test."""
    assert frozenset({"Yes"}) == PARTICIPATED


def test_not_participated_set_contents():
    assert frozenset({"No"}) == NOT_PARTICIPATED


def test_discounted_set_contests():
    """All non-participatable statuses go in DISCOUNTED."""
    assert (
        frozenset(
            {
                "Not Started",
                "Exited",
                "Voting Open",
                "No Delegated SKY",
                "Not included",
                "Pending verification",
            }
        )
        == DISCOUNTED
    )


def test_sets_are_disjoint():
    """A status belongs to at most one_bucket."""
    assert PARTICIPATED.isdisjoint(NOT_PARTICIPATED)
    assert PARTICIPATED.isdisjoint(DISCOUNTED)
    assert NOT_PARTICIPATED.isdisjoint(DISCOUNTED)


# ---------------------------------------------------------------------------
# count_statuses
# ---------------------------------------------------------------------------


def test_count_statuses_empty_input():
    counts = count_statuses([])
    assert counts == ParticipationCounts(0, 0, 0, 0)


def test_count_statuses_all_yes():
    counts = count_statuses(["Yes", "Yes", "Yes"])
    assert counts == ParticipationCounts(3, 0, 0, 0)


def test_count_statuses_all_no():
    counts = count_statuses(["No", "No", "No"])
    assert counts == ParticipationCounts(0, 3, 0, 0)


def test_count_statuses_mixed():
    counts = count_statuses(["Yes", "Yes", "No", "Not Started", "No Delegated SKY", "Not included"])
    assert counts.participated == 2
    assert counts.not_participated == 1
    assert counts.discounted == 3
    assert counts.unknown == 0


def test_count_statuses_each_discounted_status():
    statuses = list(DISCOUNTED)
    counts = count_statuses(statuses)
    assert counts.discounted == len(statuses)
    assert counts.participated == 0
    assert counts.not_participated == 0
    assert counts.unknown == 0


def test_count_statuses_unknown_status_flagged():
    """A status not in any known set is counted as unknown for operator review."""
    counts = count_statuses(["Yes", "Mystery Status", "Another One"])
    assert counts.participated == 1
    assert counts.unknown == 2


def test_count_statuses_handles_iterable_not_just_list():
    statuses = (s for s in ["Yes", "No", "Yes"])  # generator
    counts = count_statuses(statuses)
    assert counts.participated == 2
    assert counts.not_participated == 1


def test_count_status_exited_goes_to_discounted():
    counts = count_statuses(["Yes", "Exited", "Exited"])
    assert counts.participated == 1
    assert counts.discounted == 2
    assert counts.unknown == 0


def test_participation_pct_exited_excluded_from_denominator():
    assert participation_pct(["Yes", "Exited", "Exited", "Exited"]) == 1.0


def test_participation_pct_only_exited_returns_none():
    assert participation_pct(["Exited", "Exited"]) is None


# ---------------------------------------------------------------------------
# participation_pct
# ---------------------------------------------------------------------------


def test_participation_pct_all_yes_is_one():
    assert participation_pct(["Yes", "Yes", "Yes"]) == 1.0


def test_participation_pct_all_no_is_zero():
    assert participation_pct(["No", "No", "No"]) == 0.0


def test_participation_pct_half_and_half():
    assert participation_pct(["Yes", "No"]) == 0.5


def test_participation_pct_three_quarters():
    assert participation_pct(["Yes", "Yes", "No", "Yes"]) == 0.75


def test_participation_pct_empty_returns_none():
    """No data -> None ('No Data' sentinel)."""
    assert participation_pct([]) is None


def test_participation_pct_only_discounted_returns_none():
    assert participation_pct(["Not Started", "No Delegated SKY", "Not included"]) is None


def test_participation_pct_discounted_excluded_from_denominator():
    """A delegate with 1 Yes and 5 'Not Started' is at 100%, not 1/6."""
    assert (
        participation_pct(
            ["Yes", "Not Started", "Not Started", "Not Started", "Not Started", "Not Started"]
        )
        == 1.0
    )


def test_participation_pct_unknown_silently_ignored():
    """Unknown statuses don't crash and don't enter the calculation."""
    assert participation_pct(["Yes", "No", "Mystery"]) == 0.5


def test_participation_pct_realistic_numbers():
    statuses = (
        ["Yes"] * 100  # active votes
        + ["No"] * 5  # missed votes
        + ["Not Started"] * 30  # polls before the delegate's alignment startDate
        + ["No Delegated SKY"] * 10
    )  # voting window had zero SKY delegated
    pct = participation_pct(statuses)
    assert pct is not None
    assert pct == 100 / 105


# ---------------------------------------------------------------------------
# is_in_window
# ---------------------------------------------------------------------------


def test_in_window_strictly_inside():
    assert is_in_window(date(2026, 4, 15), date(2026, 4, 1), date(2026, 4, 30))


def test_in_window_on_start_boundary():
    """Inclusive on window_start."""
    assert is_in_window(date(2026, 4, 1), date(2026, 4, 1), date(2026, 4, 30))


def test_in_window_on_window_end_boundary():
    """Inclusive on window_end."""
    assert is_in_window(date(2026, 4, 30), date(2026, 4, 1), date(2026, 4, 30))


def test_not_in_window_before_start():
    assert not is_in_window(date(2026, 3, 31), date(2026, 4, 1), date(2026, 4, 30))


def test_not_in_window_after_end():
    assert not is_in_window(date(2026, 5, 1), date(2026, 4, 1), date(2026, 4, 30))


def test_in_window_unbounded_back_includes_old_polls():
    """window_start=None means 'all polls up to window_end'."""
    assert is_in_window(date(2020, 1, 1), None, date(2026, 4, 30))


def test_in_window_unbounded_back_excludes_future_polls():
    """Even with no lower bound, polls after window_end are out."""
    assert not is_in_window(date(2026, 5, 1), None, date(2026, 4, 30))


# ---------------------------------------------------------------------------
# participation_pct_for_window — integration of the two
# ---------------------------------------------------------------------------


def test_pct_for_window_filters_to_window():
    """Only polls within the window count toward the percentage."""
    poll_starts = [
        date(2026, 1, 5),  # before window
        date(2026, 4, 10),
        date(2026, 4, 20),
        date(2026, 5, 5),  # after window
    ]
    statuses = ["Yes", "Yes", "No", "Yes"]
    pct = participation_pct_for_window(poll_starts, statuses, date(2026, 4, 1), date(2026, 4, 30))
    assert pct == 0.5


def test_pct_for_window_unbounded_back_includes_all():
    """window_start = None. Pulls in all polls up to window_end."""
    poll_starts = [date(2025, 1, 1), date(2026, 1, 1), date(2026, 4, 1)]
    statuses = ["Yes", "No", "Yes"]
    pct = participation_pct_for_window(poll_starts, statuses, None, date(2026, 4, 30))
    assert pct is not None
    assert abs(pct - 2 / 3) < 1e-9


def test_pct_for_window_no_polls_in_window_returns_none():
    poll_starts = [date(2025, 1, 1), date(2025, 6, 1)]
    statuses = ["Yes", "No"]
    pct = participation_pct_for_window(poll_starts, statuses, date(2026, 4, 1), date(2026, 4, 30))
    assert pct is None


def test_pct_for_window_mismatched_lengths_raised():
    with pytest.raises(ValueError, match="same length"):
        participation_pct_for_window(
            [date(2026, 4, 1)], ["Yes", "No"], date(2026, 4, 1), date(2026, 4, 30)
        )


def test_pct_for_window_empty_inputs_returns_none():
    pct = participation_pct_for_window([], [], date(2026, 4, 1), date(2026, 4, 30))
    assert pct is None
