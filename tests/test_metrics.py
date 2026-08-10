"""Tests for the metrics module - participation percentage and window filtering."""

from datetime import date

import pytest

from ad_voting_metrics.metrics import (
    DISCOUNTED,
    NOT_PARTICIPATED,
    PARTICIPATED,
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
    assert frozenset({"No", "Late"}) == NOT_PARTICIPATED


def test_discounted_set_contents():
    """All non-participatable statuses go in DISCOUNTED.

    If the script grows a new sentinel (or the operator coins one and uses
    it in a workbook), update both the script's writer and this set, and
    pin the new contents here.
    """
    assert (
        frozenset({
            "Not Started",
            "Exited",
            "Voting Open",
            "No Delegated SKY",
            "Not included",
            "Pending verification",
            "Did not vote",
        })
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
    assert counts.participated == 0
    assert counts.not_participated == 0
    assert counts.discounted == 0
    assert counts.unknown == 0


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


# ---------------------------------------------------------------------------
# participation_pct
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["Yes", "Yes", "Yes"], 1.0),
        (["No", "No", "No"], 0.0),
        (["Yes", "No"], 0.5),
        (["Yes", "Yes", "Yes", "No"], 0.75),
    ],
)
def test_participation_pct_happy_path(statuses, expected):
    assert participation_pct(statuses) == expected


def test_participation_pct_empty_returns_none():
    """No data at all → None ('No Data' sentinel)."""
    assert participation_pct([]) is None


def test_participation_pct_only_discounted_returns_none():
    """Polls where the delegate couldn't vote don't count toward the pct."""
    assert participation_pct(["Not Started", "No Delegated SKY", "Not included"]) is None


def test_participation_pct_discounted_excluded_from_denominator():
    """A delegate with 1 Yes and 5 'Not Started' is at 100%, not 1/6."""
    assert participation_pct(["Yes", "Not Started", "Not Started", "Not Started"]) == 1.0


def test_participation_pct_late_counts_against_the_delegate():
    """A vote past the 3-business-day spell deadline earns no credit and stays in the denominator."""
    assert participation_pct(["Yes", "Late"]) == 0.5
    assert participation_pct(["Late"]) == 0.0


def test_communication_pct_discounts_late_rather_than_penalising_twice():
    """'Late' participation cross-references to 'Did not vote', which is discounted from communication."""
    assert communication_pct(["Yes", "Late"], ["Yes", "Yes"]) == 1.0


def test_participation_pct_unknown_silently_ignored():
    """Ignore unknown statuses silently rather than crashing; use count_statuses to detect them."""
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


@pytest.mark.parametrize(
    ("poll_start", "window_start", "expected"),
    [
        pytest.param(date(2026, 4, 15), date(2026, 4, 1), True, id="strictly inside"),
        pytest.param(date(2026, 4, 1), date(2026, 4, 1), True, id="inclusive on window_start"),
        pytest.param(date(2026, 4, 30), date(2026, 4, 1), True, id="inclusive on window_end"),
        pytest.param(date(2026, 3, 31), date(2026, 4, 1), False, id="before start"),
        pytest.param(date(2026, 5, 1), date(2026, 4, 1), False, id="after end"),
        pytest.param(date(2020, 1, 1), None, True, id="window_start=None includes old polls"),
        pytest.param(date(2026, 5, 1), None, False, id="window_start=None still excludes future"),
    ],
)
def test_is_in_window(poll_start, window_start, expected):
    """[window_start, window_end] membership, boundaries inclusive; None window_start is unbounded back."""
    assert is_in_window(poll_start, window_start, date(2026, 4, 30)) is expected


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
    with pytest.raises(ValueError, match=r"zip\(\) argument"):
        participation_pct_for_window(
            [date(2026, 4, 1)],
            ["Yes", "No"],
            date(2026, 4, 1),
            date(2026, 4, 30),
        )


# ---------------------------------------------------------------------------
# apply_participation_cross_reference — "No" participation overrides comm
# ---------------------------------------------------------------------------


def test_cross_ref_overrides_no_to_did_not_vote():
    """Force communication to 'Did Not Vote' when participation is 'No'."""
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
    """Mirror DISCOUNTED participation into communication to keep denominators in sync."""
    result = apply_participation_cross_reference(
        participation_statuses=["Not Started", "Exited", "Voting Open", "No Delegated SKY"],
        communication_statuses=["Yes", "No", "Yes", "Pending verification"],
    )
    assert result == ["Not Started", "Exited", "Voting Open", "No Delegated SKY"]


def test_cross_ref_mixed_participation_applies_each_rule():
    """Yes → passthrough; No → Did not vote; DISCOUNTED → mirror."""
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
    with pytest.raises(ValueError, match=r"zip\(\) argument"):
        apply_participation_cross_reference(["Yes", "No"], ["Yes"])


# ---------------------------------------------------------------------------
# communication_pct — Yes/(Yes+No) with cross-reference applied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("participation_statuses", "communication_statuses", "expected"),
    [
        (["Yes", "Yes", "Yes"], ["Yes", "Yes", "Yes"], 1.0),  # all communicated
        (["Yes", "Yes", "Yes"], ["No", "No", "No"], 0.0),  # participated, never communicated
        (["Yes", "Yes"], ["Yes", "No"], 0.5),  # half communicated
    ],
)
def test_communication_pct_happy_path(participation_statuses, communication_statuses, expected):
    pct = communication_pct(
        participation_statuses=participation_statuses,
        communication_statuses=communication_statuses,
    )
    assert pct == expected


def test_communication_pct_did_not_vote_polls_are_discounted():
    """Cross-reference 'No' to 'Did not vote'; that poll drops out of the denominator."""
    pct = communication_pct(
        participation_statuses=["Yes", "No"],
        communication_statuses=["Yes", "Yes"],  # the second is operator-recorded but overridden
    )
    assert pct == 1.0


def test_communication_pct_recorded_communication_ignored_when_did_not_vote():
    """A recorded 'Yes' on a poll the delegate didn't vote on doesn't inflate the percentage."""
    pct = communication_pct(
        participation_statuses=["No", "No", "No"],
        communication_statuses=["Yes", "Yes", "Yes"],
    )
    # All discounted → no votable polls → None
    assert pct is None


def test_communication_pct_only_discounted_participation_returns_none():
    """Return None when every poll has discounted participation."""
    pct = communication_pct(
        participation_statuses=["Not Started", "Exited", "No Delegated SKY"],
        communication_statuses=["Yes", "Yes", "Yes"],
    )
    assert pct is None


def test_communication_pct_pending_verification_in_communication_discounted():
    """Discount 'Pending verification' from the communication calculation."""
    pct = communication_pct(
        participation_statuses=["Yes", "Yes", "Yes"],
        communication_statuses=["Yes", "Pending verification", "No"],
    )
    # Only Yes and No count → 1/2 = 0.5
    assert pct == 0.5


def test_communication_pct_realistic_mix():
    """Realistic distribution: 20 voted, 3 didn't, 5 not yet aligned; comms partially reviewed."""
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
    with pytest.raises(ValueError, match=r"zip\(\) argument"):
        communication_pct_for_window(
            [date(2026, 4, 1)],
            ["Yes", "No"],
            ["Yes"],
            date(2026, 4, 1),
            date(2026, 4, 30),
        )
