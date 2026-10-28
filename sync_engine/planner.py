"""Propagation matrix: which changed field goes to which system, and when it must NOT.

Pure function of (old, new, changes, facts). Facts (is the laptop shipped? is the
Okta account active?) are looked up by the pipeline and passed in, which keeps
this module trivially testable and free of side effects.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .differ import group_of
from .models import FieldChange, Hire, PlannedOp
from .store import make_idempotency_key

SHIP_LEAD_BUSINESS_DAYS = 5
UPDATE_ACTION = {"okta": "update_user", "lumos": "update_access", "equipment": "update_request", "tracker": "update_row",
                 "workday": "update_worker"}
CREATE_ACTION = {"okta": "create_user", "lumos": "request_bundle", "equipment": "create_request", "tracker": "add_row",
                 "workday": "create_prehire"}
# Which field groups feed each system on creation (used to hold only the creates a blocked field affects).
CREATE_INPUTS = {
    "okta": {"legal_name", "preferred_name", "start_date", "work_location", "manager_email"},
    "lumos": {"start_date", "work_location", "manager_email", "department"},
    "equipment": {"legal_name", "ship_address", "start_date"},
    "tracker": {"legal_name", "start_date", "work_location", "manager_email", "department"},
    "workday": {"legal_name", "preferred_name", "personal_email", "start_date", "work_location", "manager_email",
                "department", "ship_address"},
}
# Workday is the HR system of record: Ashby-originated data must ripple into it (after validation).
# We never write a change back to the system it came from, so Workday-sourced events produce no workday ops.


@dataclass
class Facts:
    shipped: bool = False
    okta_active: bool = False


@dataclass
class PlanException:
    kind: str
    fields: list[str]
    context: dict[str, Any]
    held_ops: list[PlannedOp] = field(default_factory=list)
    held_fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class PlanResult:
    ops: list[PlannedOp] = field(default_factory=list)
    held_ops: list[PlannedOp] = field(default_factory=list)  # withheld because validation blocked a field
    exceptions: list[PlanException] = field(default_factory=list)
    deferred_groups: set[str] = field(default_factory=set)  # applied to systems only after approval


def ship_by(start: date) -> date:
    """start - 5 business days. ASSUMPTION: no holiday calendar in the prototype."""
    d, left = start, SHIP_LEAD_BUSINESS_DAYS
    while left:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def cohort_week(start: date) -> str:
    y, w, _ = start.isocalendar()
    return f"{y}-W{w:02d}"


def login_base(first: str, last: str) -> str:
    """first.last, lowercase, ASCII-folded. Collision digits are added by the Okta adapter."""
    def clean(s: str) -> str:
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
        return re.sub(r"[^a-z0-9-]", "", s)
    return f"{clean(first)}.{clean(last)}"


def region_group(h: Hire) -> str:
    return f"region-{h.work_location.country.lower()}" + ("-remote" if h.work_location.remote else "")


def app_bundle(h: Hire) -> str:
    return f"bundle:{h.department.lower()}:{h.work_location.country.lower()}"


def display_name(h: Hire) -> str:
    return h.preferred_name or f"{h.legal_first_name} {h.legal_last_name}"


class _Acc:
    """Collects per-system payload fragments into one op per (system, action)."""

    def __init__(self, hire_id: str, as_of: str):
        self.hire_id, self.as_of = hire_id, as_of
        self.parts: dict[tuple[str, str], tuple[list[str], dict[str, Any]]] = {}

    def add(self, system: str, action: str, fields: list[str], **payload: Any) -> None:
        f, p = self.parts.setdefault((system, action), ([], {}))
        f.extend(x for x in fields if x not in f)
        p.update(payload)

    def build(self) -> list[PlannedOp]:
        ops = []
        for (system, action), (fields, payload) in self.parts.items():
            # as_of (source:version) makes A->B->A reverts distinct ops instead of colliding on the key
            payload = {**payload, "as_of": self.as_of}
            ops.append(PlannedOp(hire_id=self.hire_id, system=system, action=action, fields=fields,  # type: ignore[arg-type]
                                 payload=payload, idempotency_key=make_idempotency_key(self.hire_id, system, action, payload)))
        return ops


def _as_of(new: Hire) -> str:
    return f"{new.source}:{new.source_version}"


def plan_new_hire(new: Hire, blocked: set[str]) -> PlanResult:
    acc = _Acc(new.hire_id, _as_of(new))
    acc.add("okta", CREATE_ACTION["okta"], ["legal_name", "preferred_name", "start_date", "work_location", "manager_email"],
            login_base=login_base(new.legal_first_name, new.legal_last_name), first_name=new.legal_first_name,
            last_name=new.legal_last_name, display_name=display_name(new), activation_date=new.start_date.isoformat(),
            region_group=region_group(new), manager=new.manager_email)
    acc.add("lumos", CREATE_ACTION["lumos"], ["start_date", "work_location", "manager_email", "department"],
            app_bundle=app_bundle(new), access_start=new.start_date.isoformat(), approver=new.manager_email)
    acc.add("equipment", CREATE_ACTION["equipment"], ["legal_name", "ship_address", "start_date"],
            label_name=f"{new.legal_first_name} {new.legal_last_name}", ship_address=new.ship_address.model_dump(),
            ship_by=ship_by(new.start_date).isoformat(), start_date=new.start_date.isoformat())
    acc.add("tracker", CREATE_ACTION["tracker"], ["legal_name", "start_date", "work_location", "manager_email", "department"],
            name=f"{new.legal_first_name} {new.legal_last_name}", start_date=new.start_date.isoformat(),
            cohort_week=cohort_week(new.start_date), location=new.work_location.model_dump(),
            manager=new.manager_email, department=new.department)
    if new.source == "ashby":
        acc.add("workday", "create_prehire", sorted(CREATE_INPUTS["workday"]),
                first_name=new.legal_first_name, last_name=new.legal_last_name, preferred_name=new.preferred_name,
                personal_email=new.personal_email, start_date=new.start_date.isoformat(), department=new.department,
                manager=new.manager_email, work_location=new.work_location.model_dump(),
                home_address=new.ship_address.model_dump())
    res = PlanResult()
    for op in acc.build():
        # A blocked field holds only the creates it feeds; the other systems still get the hire.
        (res.held_ops if CREATE_INPUTS[op.system] & blocked else res.ops).append(op)
    return res


def _build_update(new: Hire, old: Hire, changes: list[FieldChange], facts: Facts) -> PlanResult:
    groups = {group_of(c.field) for c in changes}
    res = PlanResult()
    acc = _Acc(new.hire_id, _as_of(new))
    name = f"{new.legal_first_name} {new.legal_last_name}"
    from_ashby = new.source == "ashby"

    def wd(target: _Acc, fields: list[str], **payload: Any) -> None:
        if from_ashby:
            target.add("workday", "update_worker", fields, **payload)

    if "start_date" in groups:
        wd(acc, ["start_date"], start_date=new.start_date.isoformat())
        s = new.start_date
        acc.add("okta", "update_user", ["start_date"], activation_date=s.isoformat())
        acc.add("lumos", "update_access", ["start_date"], access_start=s.isoformat())
        acc.add("tracker", "update_row", ["start_date"], start_date=s.isoformat(), cohort_week=cohort_week(s))
        if not facts.shipped:
            acc.add("equipment", "update_request", ["start_date"], ship_by=ship_by(s).isoformat(), start_date=s.isoformat())
        elif s < old.start_date:
            # Laptop already left and the start moved earlier: delivery risk needs a human/Claude triage.
            res.exceptions.append(PlanException(
                "late_start_change", ["start_date"],
                {"old_start": old.start_date.isoformat(), "new_start": s.isoformat(), "shipped": True}))

    if "legal_name" in groups:
        name_acc = _Acc(new.hire_id, _as_of(new))
        target = name_acc if facts.okta_active else acc
        target.add("okta", "update_user", ["legal_name"], login_base=login_base(new.legal_first_name, new.legal_last_name),
                   first_name=new.legal_first_name, last_name=new.legal_last_name, display_name=display_name(new))
        target.add("tracker", "update_row", ["legal_name"], name=name)
        wd(target, ["legal_name"], first_name=new.legal_first_name, last_name=new.legal_last_name)
        if not facts.shipped:
            target.add("equipment", "update_request", ["legal_name"], label_name=name)
        if facts.okta_active:
            # Changing the login of an active account is NEVER automatic.
            res.exceptions.append(PlanException(
                "legal_name_active_account", ["legal_first_name", "legal_last_name"],
                {"old_name": f"{old.legal_first_name} {old.legal_last_name}", "new_name": name},
                held_ops=name_acc.build(),
                held_fields={f: getattr(new, f) for f in ("legal_first_name", "legal_last_name")}))
            res.deferred_groups.add("legal_name")
    if "preferred_name" in groups:
        wd(acc, ["preferred_name"], preferred_name=new.preferred_name)
        acc.add("okta", "update_user", ["preferred_name"], display_name=display_name(new))
        acc.add("tracker", "update_row", ["preferred_name"], preferred_name=new.preferred_name)

    if "ship_address" in groups:
        wd(acc, ["ship_address"], home_address=new.ship_address.model_dump())  # HR record is correct even if the box shipped
        if facts.shipped:
            held = _Acc(new.hire_id, _as_of(new))
            # Only runs after human approval, so it carries the flag that lets the adapter accept a post-shipment change.
            held.add("equipment", "update_request", ["ship_address"], ship_address=new.ship_address.model_dump(),
                     reship_approved=True)
            res.exceptions.append(PlanException(
                "post_shipment_address", ["ship_address"], {"shipped": True, "address_changed": True},
                held_ops=held.build(), held_fields={"ship_address": new.ship_address}))
            res.deferred_groups.add("ship_address")
        else:
            acc.add("equipment", "update_request", ["ship_address"], ship_address=new.ship_address.model_dump())

    if "work_location" in groups:
        wd(acc, ["work_location"], work_location=new.work_location.model_dump())
        acc.add("okta", "update_user", ["work_location"], region_group=region_group(new))
        acc.add("lumos", "update_access", ["work_location"], app_bundle=app_bundle(new))
        acc.add("tracker", "update_row", ["work_location"], location=new.work_location.model_dump())

    if "manager_email" in groups:
        wd(acc, ["manager_email"], manager=new.manager_email)
        acc.add("okta", "update_user", ["manager_email"], manager=new.manager_email)
        acc.add("lumos", "update_access", ["manager_email"], approver=new.manager_email)
        acc.add("tracker", "update_row", ["manager_email"], manager=new.manager_email)

    if "department" in groups:
        wd(acc, ["department"], department=new.department)
    if "personal_email" in groups:
        wd(acc, ["personal_email"], personal_email=new.personal_email)
    # employment_type has no row in the propagation matrix: intentionally no ops.
    res.ops = acc.build()
    return res


def plan(old: Hire | None, new: Hire, changes: list[FieldChange], facts: Facts,
         blocked_groups: set[str] | None = None) -> PlanResult:
    blocked = blocked_groups or set()
    if old is None:
        return plan_new_hire(new, blocked)
    allowed = [c for c in changes if group_of(c.field) not in blocked]
    withheld = [c for c in changes if group_of(c.field) in blocked]
    res = _build_update(new, old, allowed, facts)
    if withheld:
        res.held_ops = _build_update(new, old, withheld, facts).ops
    return res
