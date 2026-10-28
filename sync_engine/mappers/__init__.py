"""Source payload -> canonical Hire.

Payload shapes are realistic but INVENTED (see mocks/README.md); they are not the
real Ashby or Workday schemas.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..models import Address, Hire, Location, SourceEvent


class MappingError(Exception):
    """Source payload could not be normalised (missing field, unknown enum)."""


def _addr(d: dict[str, Any] | None, postal_key: str) -> Address:
    d = d or {}
    return Address(line1=d.get("line1") or "", line2=d.get("line2"), city=d.get("city") or "",
                   state=d.get("state"), postal_code=d.get(postal_key) or "", country=d.get("country") or "")


def from_ashby(ev: SourceEvent) -> Hire:
    try:
        p = ev.payload
        c, o, loc = p["candidate"], p["offer"], p["location"]
        return Hire(
            ashby_candidate_id=c["id"], legal_first_name=c["firstName"], legal_last_name=c["lastName"],
            preferred_name=c.get("preferredName"), personal_email=c["email"],
            start_date=date.fromisoformat(o["startDate"]),
            work_location=Location(city=loc["city"], country=loc["country"], remote=bool(loc.get("remote"))),
            ship_address=_addr(p.get("shippingAddress"), "postalCode"),
            manager_email=o["hiringManagerEmail"], department=o["department"],
            employment_type=o["employmentType"], source="ashby",
            source_version=ev.source_version, updated_at=ev.occurred_at,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise MappingError(f"ashby payload: {type(e).__name__}: {e}") from e


_WD_TYPES = {"Employee": "FTE", "Contractor": "contractor", "Intern": "intern"}


def from_workday(ev: SourceEvent) -> Hire:
    try:
        w = ev.payload["worker"]
        loc = w["workLocation"]
        return Hire(
            workday_worker_id=w["workerId"], legal_first_name=w["legalName"]["first"],
            legal_last_name=w["legalName"]["last"], preferred_name=w.get("preferredName"),
            personal_email=w["contactEmail"], start_date=date.fromisoformat(w["hireDate"]),
            work_location=Location(city=loc["city"], country=loc["country"], remote=bool(loc.get("remote"))),
            ship_address=_addr(w.get("homeAddress"), "postalCode"),
            manager_email=w["managerEmail"], department=w["department"],
            employment_type=_WD_TYPES[w["workerType"]], source="workday",
            source_version=ev.source_version, updated_at=ev.occurred_at,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise MappingError(f"workday payload: {type(e).__name__}: {e}") from e


def to_hire(ev: SourceEvent) -> Hire:
    return from_ashby(ev) if ev.source == "ashby" else from_workday(ev)


def source_key(h: Hire) -> str:
    key = h.ashby_candidate_id if h.source == "ashby" else h.workday_worker_id
    assert key, "mapper guarantees the source's own id is present"
    return key
