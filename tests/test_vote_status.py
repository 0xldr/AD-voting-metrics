"""Tests for vote_status.determine_vote_status (pure logic)."""

from datetime import UTC, date, datetime

import pytest

from ad_voting_metrics import vote_status

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
