"""Validation rules V1..V8.

Each rule is a pure function. Rules only judge fields that actually changed (or a
brand-new hire), otherwise a hire whose start date has since passed would be
re-blocked by V1 on every unrelated update.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Callable

from .models import Hire, ValidationIssue

MANAGER_DOMAIN = "@betterup.co"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
POSTAL = {  # ASSUMPTION: a small set of countries is enough for the prototype
    "US": r"^\d{5}(-\d{4})?$", "CA": r"^[A-Z]\d[A-Z] ?\d[A-Z]\d$",
    "GB": r"^[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}$", "IN": r"^\d{6}$", "DE": r"^\d{5}$",
}


def _changed(new: Hire, old: Hire | None, field: str) -> bool:
    return old is None or getattr(new, field) != getattr(old, field)


def _issue(rule: str, sev: str, field: str, msg: str) -> ValidationIssue:
    return ValidationIssue(rule_id=rule, severity=sev, field=field, message=msg)  # type: ignore[arg-type]


def v1(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if not _changed(new, old, "start_date"):
        return []
    out = []
    if new.start_date < today:
        out.append(_issue("V1", "block", "start_date", f"start date {new.start_date} is in the past"))
    if new.start_date.weekday() >= 5:
        out.append(_issue("V1", "block", "start_date", f"start date {new.start_date} is not a weekday"))
    return out


def v2(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if old is None or not _changed(new, old, "start_date"):
        return []
    days = (min(old.start_date, new.start_date) - today).days
    if days <= 7:
        return [_issue("V2", "warn", "start_date",
                       f"start date changed {old.start_date} -> {new.start_date} with only {days} day(s) of lead time")]
    return []


def v3(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    out = []
    for f in ("legal_first_name", "legal_last_name", "preferred_name"):
        val = getattr(new, f)
        if val is None or not _changed(new, old, f):
            continue
        if any(ch.isdigit() for ch in val):
            out.append(_issue("V3", "block", f, f"{f} contains digits"))
        if val != val.strip():
            out.append(_issue("V3", "block", f, f"{f} has leading/trailing whitespace"))
        letters = [c for c in val if c.isalpha()]
        if len(letters) > 1 and (val.isupper() or val.islower()):
            out.append(_issue("V3", "warn", f, f"{f} is all-caps or all-lowercase"))
    return out


def v4(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if not _changed(new, old, "ship_address"):
        return []
    a, out = new.ship_address, []
    for f in ("line1", "city", "postal_code", "country"):
        if not getattr(a, f).strip():
            out.append(_issue("V4", "block", "ship_address", f"ship_address.{f} is missing"))
    pattern = POSTAL.get(a.country.upper())
    if pattern and a.postal_code and not re.match(pattern, a.postal_code.upper()):
        out.append(_issue("V4", "block", "ship_address", f"postal code {a.postal_code!r} invalid for {a.country}"))
    return out


def v5(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if new.work_location.remote or not (_changed(new, old, "work_location") or _changed(new, old, "ship_address")):
        return []
    if new.work_location.country.upper() != new.ship_address.country.upper():
        return [_issue("V5", "warn", "work_location",
                       f"work country {new.work_location.country} differs from ship country {new.ship_address.country}")]
    return []


def v6(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if _changed(new, old, "personal_email") and not EMAIL_RE.match(new.personal_email):
        return [_issue("V6", "block", "personal_email", f"invalid email {new.personal_email!r}")]
    return []


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def v7(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    """Cross-source mismatch. Routed to the Claude exception handler, not auto-resolved."""
    if other is None or other.source == new.source:
        return []
    out = []
    if (_norm(new.legal_first_name), _norm(new.legal_last_name)) != (_norm(other.legal_first_name), _norm(other.legal_last_name)):
        out.append(_issue("V7", "block", "legal_name",
                          f"{new.source} name '{new.legal_first_name} {new.legal_last_name}' != "
                          f"{other.source} '{other.legal_first_name} {other.legal_last_name}'"))
    if new.start_date != other.start_date:
        out.append(_issue("V7", "block", "start_date", f"{new.source} start {new.start_date} != {other.source} {other.start_date}"))
    if (_norm(new.work_location.city), new.work_location.country.upper()) != (_norm(other.work_location.city), other.work_location.country.upper()):
        out.append(_issue("V7", "block", "work_location", f"work location differs between {new.source} and {other.source}"))
    return out


def v8(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    if _changed(new, old, "manager_email") and not new.manager_email.lower().endswith(MANAGER_DOMAIN):
        return [_issue("V8", "block", "manager_email", f"manager {new.manager_email!r} not in {MANAGER_DOMAIN}")]
    return []


RULES: list[Callable[..., list[ValidationIssue]]] = [v1, v2, v3, v4, v5, v6, v7, v8]


def validate_all(new: Hire, old: Hire | None, other: Hire | None, today: date) -> list[ValidationIssue]:
    return [i for rule in RULES for i in rule(new, old, other, today)]
