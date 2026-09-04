"""Domain type for something delegates vote on: a SKY governance poll or an executive spell."""

from dataclasses import dataclass
from datetime import date
from typing import Literal


@dataclass(frozen=True)
class Ballot:
    """A poll or executive spell, identified the way vote.sky.money identifies it.

    `id` is the poll id as a string, or the executive spell's contract address in lowercase. Polls close at 16:00 UTC
    on `end`; spells have no end date, their voting deadline is derived from `start` (see
    `vote_status.spell_vote_deadline`).
    """

    id: str
    kind: Literal["poll", "spell"]
    start: date
    end: date | None
    title: str
