"""Tests for vote_status.determine_vote_status (pure logic)."""

from datetime import UTC, date, datetime

from ad_voting_metrics import vote_status

# ---------------------------------------------------------------------------
# determine_vote_status — voting-status logic with close-day grace period
# ---------------------------------------------------------------------------


# Standard 3-day poll spanning 4 calendar days in Dune's daily rollups:
# 16:00-24:00 on day 0, full days 1 and 2, 0:00-16:00 on day 3 (close day).
_POLL_DAYS = [date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 3), date(2026, 4, 4)]
_POLL_CLOSE = date(2026, 4, 4)
_AFTER_CLOSE = datetime(2026, 4, 10, 12, 0, tzinfo=UTC)  # a date well after poll close
_DURING_VOTING = datetime(2026, 4, 2, 12, 0, tzinfo=UTC)  # a date while poll is still open


def _sky_dict(day0: float, day1: float, day2: float, day3: float) -> dict:
    return dict(zip(_POLL_DAYS, [day0, day1, day2, day3], strict=True))


def test_status_no_sky_anywhere_returns_no_delegated_sky():
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_empty_sky_dict_returns_no_delegated_sky():
    """Empty sky dict treated as all-zero rather than falling through to a stale value."""
    assert (
        vote_status.determine_vote_status(
            {},
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_voted_with_sky_returns_yes():
    """Delegate had SKY and voted."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
        )
        == "Yes"
    )


def test_status_did_not_vote_full_window_returns_no():
    """Delegate has SKY throughout and did not vote."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No"
    )


def test_status_grace_period_close_day_only_no_vote_returns_no_delegated_sky():
    """Scenario C: zero days 0-2, non-zero only on close day, didn't vote → grace applies."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_grace_period_close_day_only_voted_returns_yes():
    """Scenario D: same as C but they voted anyway — counts as Yes."""
    sky = _sky_dict(0, 0, 0, 1000)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
        )
        == "Yes"
    )


def test_status_pre_close_day_delegation_still_returns_no():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 0, 1000, 1000)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No"
    )


def test_status_mid_window_only_returns_no_delegated_sky():
    """Non-zero on days 2-3, didn't vote → No (both rule conditions met)."""
    sky = _sky_dict(0, 1000, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_voted_but_withdrew_before_close_returns_yes():
    """A recorded vote stands even if delegations were pulled before close."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_AFTER_CLOSE,
        )
        == "Yes"
    )


def test_status_grief_vector_blocked():
    """A hostile delegator dropping 1 SKY on close day cannot harm the target.

    Without grace rule: target marked No, participation % drops,
    eligibility may flip. With it: marked No Delegated SKY, discounted,
    target unaffected.
    """
    sky = _sky_dict(0, 0, 0, 1)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_mid_window_withdrawal_returns_no_delegated_sky():
    """Withdrawn-before-close + no vote → No Delegated SKY, not No.

    Zero stake at the decisive moment → discounted, not penalised.
    """
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_date_uses_what_is_present():
    """Missing days are treated as zero; only day 3 has a row → grace applies."""
    sky = {date(2026, 4, 4): 1000.0}
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


def test_status_partial_window_pre_close_only_returns_no_delegated_sky():
    """Day-1 only (close day missing → treated as zero) → No Delegated SKY."""
    sky = {date(2026, 4, 2): 1000.0}
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_AFTER_CLOSE,
        )
        == "No Delegated SKY"
    )


# ---------------------------------------------------------------------------
# determine_vote_status — in-progress polls (current_datetime < poll_end_date)
# ---------------------------------------------------------------------------


def test_status_in_progress_poll_voted_returns_yes():
    """Delegate already voted on a poll still open. Counted positively."""
    sky = _sky_dict(1000, 1000, 0, 0)  # data through day 1 only
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_DURING_VOTING,
        )
        == "Yes"
    )


def test_status_in_progress_poll_not_voted_returns_voting_open():
    """Open poll, no vote → 'Voting Open' (discounted, not penalised)."""
    sky = _sky_dict(1000, 1000, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_DURING_VOTING,
        )
        == "Voting Open"
    )


def test_status_in_progress_with_no_sky_still_returns_voting_open():
    """Open poll with no SKY now → still 'Voting Open' (they could still receive a delegation)."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=_DURING_VOTING,
        )
        == "Voting Open"
    )


def test_status_in_progress_voted_with_no_sky_returns_yes():
    """Recorded vote stands even with zero SKY at run time — the API's voted flag is authoritative."""
    sky = _sky_dict(0, 0, 0, 0)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=True,
            current_datetime=_DURING_VOTING,
        )
        == "Yes"
    )


def test_status_close_day_before_1600_utc_treated_as_open():
    """15:00 UTC on close day → still open (close is 16:00 UTC)."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "Voting Open"
    )


def test_status_close_day_at_exactly_1600_utc_treated_as_closed():
    """Exactly 16:00 UTC counts as closed; close-day rule applies."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 16, 0, tzinfo=UTC)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "No"
    )


def test_status_close_day_after_1600_utc_treated_as_closed():
    """17:00 UTC on close day → closed."""
    sky = _sky_dict(1000, 1000, 1000, 1000)
    current = datetime(2026, 4, 4, 17, 0, tzinfo=UTC)
    assert (
        vote_status.determine_vote_status(
            sky,
            _POLL_CLOSE,
            delegate_voted=False,
            current_datetime=current,
        )
        == "No"
    )
