"""Field-level diff against last-applied state."""
from __future__ import annotations

from .models import FieldChange, Hire

TRACKED = (
    "legal_first_name", "legal_last_name", "preferred_name", "personal_email", "start_date",
    "work_location", "ship_address", "manager_email", "department", "employment_type",
)
LEGAL_NAME = {"legal_first_name", "legal_last_name"}


def group_of(field: str) -> str:
    """First/last name change together as one identity concern."""
    return "legal_name" if field in LEGAL_NAME else field


def diff(old: Hire | None, new: Hire) -> list[FieldChange]:
    """Only genuinely changed fields; a new hire (old=None) yields every tracked field."""
    new_d = new.model_dump(mode="json")
    old_d = old.model_dump(mode="json") if old else None
    return [
        FieldChange(field=f, old=old_d[f] if old_d else None, new=new_d[f])
        for f in TRACKED
        if old_d is None or old_d[f] != new_d[f]
    ]
