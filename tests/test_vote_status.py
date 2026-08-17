"""Tests for vote_status — the status vocabulary, poll close-day statuses, and the spell voting deadline."""

from datetime import UTC, date, datetime

import pytest

from ad_voting_metrics import vote_status
from ad_voting_metrics.vote_status import (
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


def test_status_constants_match_their_set_membership():
    """The named constants and the sets can't drift apart."""
    assert vote_status.YES in PARTICIPATED
    assert {vote_status.NO, vote_status.LATE} <= NOT_PARTICIPATED
    assert {
        vote_status.NOT_STARTED,
        vote_status.EXITED,
        vote_status.VOTING_OPEN,
        vote_status.NO_DELEGATED_SKY,
        vote_status.NOT_INCLUDED,
        vote_status.PENDING_VERIFICATION,
        vote_status.DID_NOT_VOTE,
    } <= DISCOUNTED


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


# ---------------------------------------------------------------------------
# determine_vote_status
# ---------------------------------------------------------------------------

# Standard 3-day poll spanning 4 calendar days in daily SKY delegation snapshots:
# 16:00-24:00 on day 0, full days 1 and 2, 0:00-16:00 on day 3 (close day).
_POLL_DAYS = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3), date(2026, 4, 4)]
_POLL_CLOSE = date(2026, 4, 4)

_AFTER_CLOSE = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)
_DURING_VOTING = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)
_CLOSE_DAY_BEFORE_1600 = datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
_CLOSE_DAY_AT_1600 = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
_CLOSE_DAY_AFTER_1600 = datetime(2026, 4, 4, 17, 0, tzinfo=UTC)


def _sky(*values: float) -> dict[date, float]:
    """Pair _POLL_DAYS with the given per-day SKY balances."""
    return dict(zip(_POLL_DAYS, values, strict=True))


@pytest.mark.parametrize(
    ("sky", "delegate_voted", "current_datetime", "expected"),
    [
        # --- Closed poll: no/zero SKY → No Delegated SKY -------------------
        pytest.param(_sky(0, 0, 0, 0), False, _AFTER_CLOSE, "No Delegated SKY", id="no sky anywhere"),
        pytest.param({}, False, _AFTER_CLOSE, "No Delegated SKY", id="empty sky dict"),
        # --- Closed poll: sky throughout window ----------------------------
        pytest.param(_sky(1000, 1000, 1000, 1000), True, _AFTER_CLOSE, "Yes", id="voted with sky throughout"),
        pytest.param(_sky(1000, 1000, 1000, 1000), False, _AFTER_CLOSE, "No", id="did not vote with sky throughout"),
        # --- Closed poll: grace period (no sky before close day) -----------
        pytest.param(_sky(0, 0, 0, 1000), False, _AFTER_CLOSE, "No Delegated SKY", id="close-day-only sky, no vote"),
        pytest.param(_sky(0, 0, 0, 1000), True, _AFTER_CLOSE, "Yes", id="close-day-only sky, voted"),
        pytest.param(_sky(0, 0, 1000, 1000), False, _AFTER_CLOSE, "No", id="pre-close-day sky persists, no vote"),
        pytest.param(_sky(0, 1000, 0, 0), False, _AFTER_CLOSE, "No Delegated SKY", id="mid-window only"),
        pytest.param(_sky(1000, 1000, 0, 0), True, _AFTER_CLOSE, "Yes", id="withdrew pre-close, voted"),
        pytest.param(_sky(1000, 1000, 0, 0), False, _AFTER_CLOSE, "No Delegated SKY", id="withdrew pre-close, no vote"),
        pytest.param(_sky(0, 0, 0, 1), False, _AFTER_CLOSE, "No Delegated SKY", id="grief vector: 1 SKY on close day"),
        # --- Closed poll: partial sky data ---------------------------------
        pytest.param(
            {date(2026, 4, 4): 1000.0},
            False,
            _AFTER_CLOSE,
            "No Delegated SKY",
            id="only close day present",
        ),
        pytest.param(
            {date(2026, 4, 2): 1000.0},
            False,
            _AFTER_CLOSE,
            "No Delegated SKY",
            id="only pre-close day present",
        ),
        # --- Open poll: current_datetime < 16:00 UTC on close day ----------
        pytest.param(_sky(1000, 1000, 0, 0), True, _DURING_VOTING, "Yes", id="open poll, voted"),
        pytest.param(_sky(1000, 1000, 0, 0), False, _DURING_VOTING, "Voting Open", id="open poll, not voted"),
        pytest.param(_sky(0, 0, 0, 0), False, _DURING_VOTING, "Voting Open", id="open poll, no sky"),
        pytest.param(_sky(0, 0, 0, 0), True, _DURING_VOTING, "Yes", id="open poll, voted with no sky"),
        # --- Close-day time-of-day boundary --------------------------------
        pytest.param(
            _sky(1000, 1000, 1000, 1000),
            False,
            _CLOSE_DAY_BEFORE_1600,
            "Voting Open",
            id="15:00 UTC → still open",
        ),
        pytest.param(
            _sky(1000, 1000, 1000, 1000),
            False,
            _CLOSE_DAY_AT_1600,
            "No",
            id="16:00 UTC → closed",
        ),
        pytest.param(
            _sky(1000, 1000, 1000, 1000),
            False,
            _CLOSE_DAY_AFTER_1600,
            "No",
            id="17:00 UTC → closed",
        ),
    ],
)
def test_determine_vote_status(sky, delegate_voted, current_datetime, expected):
    """Status rule for one (delegate, poll) pair across closed and open-poll regimes."""
    result = vote_status.determine_vote_status(
        sky,
        _POLL_CLOSE,
        delegate_voted=delegate_voted,
        current_datetime=current_datetime,
    )
    assert result == expected


# ---------------------------------------------------------------------------
# spell_vote_deadline
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spell_start", "expected"),
    [
        pytest.param(date(2026, 4, 6), date(2026, 4, 9), id="Mon -> Thu"),
        pytest.param(date(2026, 4, 7), date(2026, 4, 10), id="Tue -> Fri"),
        pytest.param(date(2026, 4, 1), date(2026, 4, 6), id="Wed -> Mon, weekend skipped"),
        pytest.param(date(2026, 4, 2), date(2026, 4, 7), id="Thu -> Tue, weekend skipped"),
        pytest.param(date(2026, 4, 3), date(2026, 4, 8), id="Fri -> Wed, weekend skipped"),
        pytest.param(date(2026, 4, 4), date(2026, 4, 8), id="Sat -> Wed, Monday is day 1"),
        pytest.param(date(2026, 4, 5), date(2026, 4, 8), id="Sun -> Wed, Monday is day 1"),
    ],
)
def test_spell_vote_deadline_skips_weekends(spell_start, expected):
    """Three Mon-Fri days strictly after the spell goes live; no holiday calendar."""
    assert vote_status.spell_vote_deadline(spell_start) == expected


def test_spell_vote_deadline_honours_explicit_business_days():
    """The window length is a parameter; the default is SPELL_VOTE_BUSINESS_DAYS."""
    assert vote_status.SPELL_VOTE_BUSINESS_DAYS == 3
    # Friday + 1 business day is the following Monday.
    assert vote_status.spell_vote_deadline(date(2026, 4, 3), business_days=1) == date(2026, 4, 6)


def test_spell_vote_deadline_spanning_a_month_boundary():
    """Deadlines roll into the next month; a spell late in the month is still adjudicated."""
    # Thursday 2026-04-30 -> Fri, (weekend), Mon, Tue = 2026-05-05.
    assert vote_status.spell_vote_deadline(date(2026, 4, 30)) == date(2026, 5, 5)
