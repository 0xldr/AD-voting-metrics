"""Status vocabulary for SKY poll and executive-spell participation.

`fetch` writes one status per (poll, delegate) into the workbook's Participation Raw Data tab and seeds the matching
Communication Master cell from it. The strings and the sets that classify them live here so that every producer (the
sources modules) and consumer (the sheets writers) shares one definition.

## Status vocabulary

Each status is per-(poll, delegate). The same poll can have different statuses for different delegates depending on
alignment timing and SKY balance during the voting window:

  - "Yes": delegate had SKY delegated at some point during the voting window and cast a vote.
  - "No": delegate had SKY delegated at some point during the voting window but did not vote.
  - "Late": spell-only. The delegate voted for the executive, but after the 3-business-day deadline (see
    vote_status.spell_vote_deadline). Counted as non-participation - a vote past the deadline earns no credit - but
    labelled distinctly from "No" so the operator can tell "voted late" from "never voted".
  - "Not Started": the delegate's alignment start_date is after this poll's end date - they weren't aligned yet when the
    poll closed, so non-participation isn't held against them.
  - "Exited": the delegate's alignment endDate is before this poll's start_date - they had already exited when the poll
    opened. Symmetric to "Not Started" on the other temporal boundary.
  - "Voting Open": the poll's voting window had not yet closed at the time the data was fetched. Discounted so
    non-voters aren't penalized until the poll closes and the data is re-run.
  - "No Delegated SKY": the delegate had zero SKY delegated to them on every day of the poll's voting window.
  - "Not included": operator-flagged poll to exclude (governance exception, data error) - set manually, not by the
    script.
  - "Pending verification": spell vote recorded but not yet verified; also used in Communication Master for cells
    awaiting operator review.
  - "Did not vote": derived value used in communication when participation = "No". Only appears in communication
    contexts.

PARTICIPATED, NOT_PARTICIPATED, and DISCOUNTED partition the vocabulary into counts-for, counts-against, and
counts-for-neither. A status in none of the three is unrecognized.
"""

# Spell-vote default for the (delegate participated) + (not yet verified)
# case. Shared across sheets, sky_executive, and sky_executive_onchain so
# the string only lives in one place.
PENDING_VERIFICATION = "Pending verification"

# Spell vote cast after the 3-business-day deadline. Shared with
# sky_executive_onchain, which is the only producer.
LATE = "Late"

# Status sets. Frozensets for 0(1) membership and immutability - adding
# a new status (e.g. a new exclusion category) means updating one of these.
PARTICIPATED = frozenset({"Yes"})

NOT_PARTICIPATED = frozenset({"No", LATE})

DISCOUNTED = frozenset(
    {
        "Not Started",
        "Exited",
        "Voting Open",
        "No Delegated SKY",
        "Not included",
        PENDING_VERIFICATION,
        "Did not vote",
    }
)


def cross_reference_one(participation: str) -> str | None:
    """Return the implied communication value for a participation status, or None for passthrough.

      - participation in NOT_PARTICIPATED → "Did not vote"
      - participation in DISCOUNTED → mirror the participation status
      - participation in PARTICIPATED or unknown → None (caller chooses passthrough or default)

    A late vote earns no participation credit, so its rationale is discounted from communication too rather than
    penalised a second time.

    Returns:
        The override value, or None when the caller should keep its existing communication value.
    """
    if participation in NOT_PARTICIPATED:
        return "Did not vote"
    if participation in DISCOUNTED:
        return participation
    return None
