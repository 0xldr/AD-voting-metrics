"""Tests for the metrics module - status sets and the participation cross-reference rule."""

import pytest

from ad_voting_metrics.metrics import (
    DISCOUNTED,
    NOT_PARTICIPATED,
    PARTICIPATED,
    cross_reference_one,
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
# cross_reference_one — participation status implies a communication value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("participation", sorted(NOT_PARTICIPATED))
def test_cross_ref_non_participation_becomes_did_not_vote(participation):
    """Both "No" and "Late" override communication to "Did not vote".

    A late vote earns no participation credit, so discounting its rationale avoids penalising it twice.
    """
    assert cross_reference_one(participation) == "Did not vote"


@pytest.mark.parametrize("participation", sorted(DISCOUNTED))
def test_cross_ref_mirrors_discounted_participation(participation):
    """DISCOUNTED participation is mirrored into communication to keep denominators in sync."""
    assert cross_reference_one(participation) == participation


@pytest.mark.parametrize("participation", sorted(PARTICIPATED))
def test_cross_ref_participation_passes_through(participation):
    """A participating delegate's communication value is the caller's to decide."""
    assert cross_reference_one(participation) is None


def test_cross_ref_unknown_status_passes_through():
    """An unrecognized status is not silently reclassified."""
    assert cross_reference_one("Mystery Status") is None


def test_cross_ref_empty_status_passes_through():
    assert cross_reference_one("") is None
