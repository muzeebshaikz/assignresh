"""Identity matching: which internal hire does this source record belong to?"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .mappers import source_key
from .models import Hire
from .store import Store

FUZZY_THRESHOLD = 0.85


@dataclass
class MatchResult:
    kind: str  # existing | new | ambiguous
    hire_id: str | None = None
    reason: str = ""


def _full(h: Hire) -> str:
    return f"{h.legal_first_name} {h.legal_last_name}".lower().strip()


def match(store: Store, incoming: Hire, force: str | None = None) -> MatchResult:
    """Resolve identity. `force` is a human decision: a hire_id to link to, or 'new'."""
    if force == "new":
        return MatchResult("new", reason="human decided: different person")
    if force:
        return MatchResult("existing", force, "human decided: same person")

    rec = store.get_source_record(incoming.source, source_key(incoming))
    if rec:
        return MatchResult("existing", rec["hire_id"], "known source id")

    hires = store.all_hires()
    for h in hires:
        if h.personal_email.lower() == incoming.personal_email.lower():
            return MatchResult("existing", h.hire_id, "same personal email")

    # Similar name but different email: could be a typo'd duplicate or a namesake. Don't guess.
    for h in hires:
        if SequenceMatcher(None, _full(h), _full(incoming)).ratio() >= FUZZY_THRESHOLD:
            return MatchResult("ambiguous", h.hire_id, f"name similar to {h.hire_id} but email differs")
    return MatchResult("new", reason="no match: off-cycle or first-seen hire")
