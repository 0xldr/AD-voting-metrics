"""Tests for the metrics module - participation percentage and window filtering."""

from datetime import date

import pytest

from ad_voting_metrics.metrics import (
    DISCOUNTED,
    NOT_PARTICIPATED,
    PARTICIPATED,
    ParticipationCounts,
    apply_participation_cross_reference,
    communication_pct,
    communication_pct_for_window,
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


def test_discounted_set_contents():
    """All non-participatable statuses go in DISCOUNTED.

    If the script grows a new sentinel (or the operator coins one and uses
    it in a workbook), update both the script's writer and this set, and
    pin the new contents here.
    """
    assert (
        frozenset(
            {
                "Not Started",
                "Exited",
                "Voting Open",
                "No Delegated SKY",
                "Not included",
                "Pending verification",
                "Did not vote",
            }
        )
        == DISCOUNTED
    )


def test_status_sets_are_disjoint():
    """A status string belongs to at most one bucket — no overlap."""
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
    assert counts.participated == 3
    assert counts.not_participated == 0
    assert counts.discounted == 0
    assert counts.unknown == 0


def test_count_statuses_all_no():
    counts = count_statuses(["No", "No"])
    assert counts.not_participated == 2
    assert counts.participated == 0


def test_count_statuses_mixed():
    counts = count_statuses(["Yes", "Yes", "No", "Not Started", "No Delegated SKY", "Not included"])
    assert counts.participated == 2
    assert counts.not_participated == 1
    assert counts.discounted == 3
    assert counts.unknown == 0


def test_count_statuses_each_discounted_status():
    """Every status in DISCOUNTED ends up in the discounted bucket."""
    statuses = list(DISCOUNTED)
    counts = count_statuses(statuses)
    assert counts.discounted == len(statuses)
    assert counts.participated == 0
    assert counts.not_participated == 0
    assert counts.unknown == 0


def test_count_statuses_unknown_statuses_flagged():
    """A status not in any known set is counted as unknown for operator review."""
    counts = count_statuses(["Yes", "Mystery Status", "Another One"])
    assert counts.participated == 1
    assert counts.unknown == 2


def test_count_statuses_exited_goes_to_discounted():
    """Count 'Exited' as discounted, symmetric with 'Not Started'.

    Exited means the delegate exited before the poll started; Not Started
    means they aligned after the poll ended. Both temporal-boundary
    statuses belong in discounted.
    """
    counts = count_statuses(["Yes", "Exited", "Exited"])
    assert counts.participated == 1
    assert counts.discounted == 2
    assert counts.unknown == 0


def test_participation_pct_exited_excluded_from_denominator():
    """Exclude Exited polls from the percentage denominator.

    A delegate isn't penalized for polls that opened after their exit.
    """
    assert participation_pct(["Yes", "Exited", "Exited", "Exited"]) == 1.0


def test_participation_pct_only_exited_returns_none():
    """A delegate whose entire window is Exited has no votable polls → None."""
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
    assert participation_pct(["Yes", "Yes", "Yes", "No"]) == 0.75


def test_participation_pct_empty_returns_none():
    """No data at all → None ('No Data' sentinel)."""
    assert participation_pct([]) is None


def test_participation_pct_only_discounted_returns_none():
    """Polls where the delegate couldn't vote don't count toward the pct."""
    assert participation_pct(["Not Started", "No Delegated SKY", "Not included"]) is None


def test_participation_pct_discounted_excluded_from_denominator():
    """A delegate with 1 Yes and 5 'Not Started' is at 100%, not 1/6."""
    assert participation_pct(["Yes", "Not Started", "Not Started", "Not Started"]) == 1.0


def test_participation_pct_unknown_silently_ignored():
    """Ignore unknown statuses silently rather than crashing.

    Use count_statuses if you need to detect them.
    """
    assert participation_pct(["Yes", "No", "Mystery"]) == 0.5


def test_participation_pct_realistic_workbook_mix():
    """Mirrors the kind of distribution seen in the actual April 2026 data."""
    statuses = (
        ["Yes"] * 100  # active votes
        + ["No"] * 5  # missed votes
        + ["Not Started"] * 30  # polls before delegate's alignment startDate
        + ["No Delegated SKY"] * 10  # voting window had zero SKY delegated
    )
    pct = participation_pct(statuses)
    assert pct is not None
    assert pct == 100 / 105


# ---------------------------------------------------------------------------
# is_in_window
# ---------------------------------------------------------------------------


def test_in_window_strictly_inside():
    assert is_in_window(date(2026, 4, 15), date(2026, 4, 1), date(2026, 4, 30))


def test_in_window_on_window_start_boundary():
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
        date(2026, 4, 10),  # in window
        date(2026, 4, 20),  # in window
        date(2026, 5, 5),  # after window
    ]
    statuses = ["Yes", "Yes", "No", "Yes"]
    pct = participation_pct_for_window(poll_starts, statuses, date(2026, 4, 1), date(2026, 4, 30))
    # In-window: ["Yes", "No"] → 0.5
    assert pct == 0.5


def test_pct_for_window_unbounded_back_includes_all():
    """window_start=None pulls in all polls up to window_end."""
    poll_starts = [date(2025, 1, 1), date(2026, 1, 1), date(2026, 4, 1)]
    statuses = ["Yes", "No", "Yes"]
    pct = participation_pct_for_window(poll_starts, statuses, None, date(2026, 4, 30))
    # All three count → 2/3
    assert pct is not None
    assert abs(pct - 2 / 3) < 1e-9


def test_pct_for_window_no_polls_in_window_returns_none():
    poll_starts = [date(2025, 1, 1), date(2025, 6, 1)]
    statuses = ["Yes", "No"]
    pct = participation_pct_for_window(poll_starts, statuses, date(2026, 4, 1), date(2026, 4, 30))
    assert pct is None


def test_pct_for_window_only_discounted_in_window_returns_none():
    poll_starts = [date(2026, 4, 5), date(2026, 4, 15)]
    statuses = ["Not Started", "Not Started"]
    pct = participation_pct_for_window(poll_starts, statuses, date(2026, 4, 1), date(2026, 4, 30))
    assert pct is None


def test_pct_for_window_mismatched_lengths_raises():
    """Bug-prone caller error fails fast rather than silently truncating."""
    with pytest.raises(ValueError, match="same length"):
        participation_pct_for_window(
            [date(2026, 4, 1)],
            ["Yes", "No"],
            date(2026, 4, 1),
            date(2026, 4, 30),
        )


def test_pct_for_window_empty_inputs_returns_none():
    pct = participation_pct_for_window([], [], date(2026, 4, 1), date(2026, 4, 30))
    assert pct is None


# ---------------------------------------------------------------------------
# apply_participation_cross_reference — "No" participation overrides comm
# ---------------------------------------------------------------------------


def test_cross_ref_overrides_no_to_did_not_vote():
    """Force communication to 'Did Not Vote' when participation is 'No'.

    Overrides anything else that has been recorded.
    """
    result = apply_participation_cross_reference(
        participation_statuses=["No", "No", "No"],
        communication_statuses=["Yes", "No", "Yes"],
    )
    assert result == ["Did not vote", "Did not vote", "Did not vote"]


def test_cross_ref_preserves_yes_participation_communication():
    """For participation = 'Yes', communication passes through."""
    result = apply_participation_cross_reference(
        participation_statuses=["Yes", "Yes", "Yes"],
        communication_statuses=["Yes", "No", "Pending verification"],
    )
    assert result == ["Yes", "No", "Pending verification"]


def test_cross_ref_mirrors_discounted_participation_to_communication():
    """Mirror DISCOUNTED participation statuses into communication.

    Polls oustide the delegate's alignment period (or otherwise
    ineligible for participation) can't contribute to a communication
    metric either; mirroring keeps both metrics' denominators in sync.
    """
    result = apply_participation_cross_reference(
        participation_statuses=["Not Started", "Exited", "Voting Open", "No Delegated SKY"],
        communication_statuses=["Yes", "No", "Yes", "Pending verification"],
    )
    assert result == ["Not Started", "Exited", "Voting Open", "No Delegated SKY"]


def test_cross_ref_mixed_participation_applies_each_rule():
    """Apply the right transformation per status in a mixed import.

    Yes → passthrough; No → Did not vote; DISCOUNTED → mirror.
    """
    result = apply_participation_cross_reference(
        participation_statuses=["Yes", "No", "Not Started", "No", "Yes"],
        communication_statuses=["Yes", "Yes", "Pending verification", "No", "No"],
    )
    assert result == ["Yes", "Did not vote", "Not Started", "Did not vote", "No"]


def test_cross_ref_does_not_mutate_inputs():
    """The function returns a new list; original sequences unchanged."""
    participation = ["No", "Yes"]
    communication = ["Yes", "Yes"]
    apply_participation_cross_reference(participation, communication)
    assert participation == ["No", "Yes"]
    assert communication == ["Yes", "Yes"]


def test_cross_ref_empty_inputs_returns_empty():
    assert apply_participation_cross_reference([], []) == []


def test_cross_ref_mismatched_lengths_raises():
    with pytest.raises(ValueError, match="same length"):
        apply_participation_cross_reference(["Yes", "No"], ["Yes"])


# ---------------------------------------------------------------------------
# communication_pct — Yes/(Yes+No) with cross-reference applied
# ---------------------------------------------------------------------------


def test_communication_pct_all_yes_yes_is_one():
    """Both participated and communicated on every poll."""
    pct = communication_pct(
        participation_statuses=["Yes", "Yes", "Yes"],
        communication_statuses=["Yes", "Yes", "Yes"],
    )
    assert pct == 1.0


def test_communication_pct_all_yes_no_is_zero():
    """Participated everywhere, didn't communicate anywhere."""
    pct = communication_pct(
        participation_statuses=["Yes", "Yes", "Yes"],
        communication_statuses=["No", "No", "No"],
    )
    assert pct == 0.0


def test_communication_pct_half_communicated():
    pct = communication_pct(
        participation_statuses=["Yes", "Yes"],
        communication_statuses=["Yes", "No"],
    )
    assert pct == 0.5


def test_communication_pct_did_not_vote_polls_are_discounted():
    """Discount 'Did not vote' polls from both numerator and denominator.

    Polls where participation = 'No' get cross-referenced to 'Did not
    vote'. A delegate with 1 communicated and 1 not-voted scores 100%,
    not 50%.
    """
    pct = communication_pct(
        participation_statuses=["Yes", "No"],
        communication_statuses=["Yes", "Yes"],  # the second is operator-recorded but overridden
    )
    assert pct == 1.0


def test_communication_pct_recorded_communication_ignored_when_did_not_vote():
    """Override recorded communication when the delegate didn't vote.

    A recorded 'Yes' on a poll the delegate didn't vote on doesn't
    inflate the percentage - the cross-reference overrides it.
    """
    pct = communication_pct(
        participation_statuses=["No", "No", "No"],
        communication_statuses=["Yes", "Yes", "Yes"],
    )
    # All discounted → no votable polls → None
    assert pct is None


def test_communication_pct_only_discounted_participation_returns_none():
    """Return None when every poll has discounted participation.

    No votable polls means communication has nothing to count.
    """
    pct = communication_pct(
        participation_statuses=["Not Started", "Exited", "No Delegated SKY"],
        communication_statuses=["Yes", "Yes", "Yes"],
    )
    assert pct is None


def test_communication_pct_pending_verification_in_communication_discounted():
    """Discount 'Pending verification' from the communication calculation.

    The operator hasn't reviewed the call yet; it stays out until they do.
    """
    pct = communication_pct(
        participation_statuses=["Yes", "Yes", "Yes"],
        communication_statuses=["Yes", "Pending verification", "No"],
    )
    # Only Yes and No count → 1/2 = 0.5
    assert pct == 0.5


def test_communication_pct_realistic_mix():
    """Exercise a realistic distribution of statuses.

    Most polls had participation, a few didn't, and operator has
    reviewed most communication cells.
    """
    pct = communication_pct(
        participation_statuses=(
            ["Yes"] * 20  # voted on 20 polls
            + ["No"] * 3  # didn't vote on 3
            + ["Not Started"] * 5  # 5 polls before alignment
        ),
        communication_statuses=(
            ["Yes"] * 18
            + ["No"] * 2  # 18 communicated, 2 didn't on voted polls
            + ["Yes"] * 3  # operator-recorded on didn't-vote (overridden)
            + ["Pending verification"] * 5  # 5 not-yet-reviewed (discounted anyway)
        ),
    )
    # Voted polls: 18 Yes + 2 No = 20 denominator; 18/20 = 0.9
    # Didn't-vote polls: overridden to "Did not vote", discounted
    # Not Started polls: communication discounted regardless
    assert pct is not None
    assert pct == 0.9


def test_communication_pct_empty_inputs_returns_none():
    assert communication_pct([], []) is None


def test_communication_pct_mismatched_lengths_raises():
    with pytest.raises(ValueError, match="same length"):
        communication_pct(["Yes", "No"], ["Yes"])


# ---------------------------------------------------------------------------
# communication_pct_for_window — window-filtered
# ---------------------------------------------------------------------------


def test_communication_pct_for_window_filters_to_window():
    """Filter out-of-window polls before cross-reference and calculation."""
    poll_starts = [
        date(2026, 1, 5),  # before window
        date(2026, 4, 10),  # in window
        date(2026, 4, 20),  # in window
        date(2026, 5, 5),  # after window
    ]
    participation = ["Yes", "Yes", "No", "Yes"]
    communication = ["Yes", "Yes", "Yes", "No"]
    pct = communication_pct_for_window(
        poll_starts,
        participation,
        communication,
        date(2026, 4, 1),
        date(2026, 4, 30),
    )
    # In-window: participation=[Yes, No], communication=[Yes, Yes]
    # After cross-ref: communication=[Yes, "Did not vote"]
    # Did not vote discounted → 1 Yes / 1 denominator = 1.0
    assert pct == 1.0


def test_communication_pct_for_window_no_polls_in_window_returns_none():
    poll_starts = [date(2025, 1, 1), date(2025, 6, 1)]
    participation = ["Yes", "Yes"]
    communication = ["Yes", "Yes"]
    pct = communication_pct_for_window(
        poll_starts,
        participation,
        communication,
        date(2026, 4, 1),
        date(2026, 4, 30),
    )
    assert pct is None


def test_communication_pct_for_window_unbounded_back():
    """window_start=None includes all polls up to window_end."""
    poll_starts = [date(2024, 1, 1), date(2025, 6, 1), date(2026, 4, 1)]
    participation = ["Yes", "Yes", "Yes"]
    communication = ["Yes", "No", "Yes"]
    pct = communication_pct_for_window(
        poll_starts,
        participation,
        communication,
        None,
        date(2026, 4, 30),
    )
    # All three count → 2 Yes / 3 = 2/3
    assert pct is not None
    assert abs(pct - 2 / 3) < 1e-9


def test_communication_pct_for_window_mismatched_lengths_raises():
    with pytest.raises(ValueError, match="same length"):
        communication_pct_for_window(
            [date(2026, 4, 1)],
            ["Yes", "No"],
            ["Yes"],
            date(2026, 4, 1),
            date(2026, 4, 30),
        )


def test_communication_pct_for_window_empty_returns_none():
    pct = communication_pct_for_window(
        [],
        [],
        [],
        date(2026, 4, 1),
        date(2026, 4, 30),
    )
    assert pct is None
