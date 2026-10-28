"""Canonical data model.

Every source payload is normalised into `Hire` so the rest of the pipeline never
needs to know whether a change came from Ashby or Workday.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class Location(BaseModel):
    city: str
    country: str
    remote: bool = False


class Address(BaseModel):
    line1: str = ""
    line2: str | None = None
    city: str = ""
    state: str | None = None
    postal_code: str = ""
    country: str = ""


class Hire(BaseModel):
    hire_id: str = ""  # assigned by the matcher; empty until identity is resolved
    ashby_candidate_id: str | None = None
    workday_worker_id: str | None = None
    legal_first_name: str
    legal_last_name: str
    preferred_name: str | None = None
    personal_email: str
    start_date: date
    work_location: Location
    ship_address: Address
    manager_email: str
    department: str
    employment_type: Literal["FTE", "contractor", "intern"]
    source: Literal["ashby", "workday"]
    source_version: int
    updated_at: datetime


class SourceEvent(BaseModel):
    event_id: str
    source: Literal["ashby", "workday"]
    event_type: Literal["hire.created", "hire.updated"]
    payload: dict[str, Any]
    source_version: int
    occurred_at: datetime


class FieldChange(BaseModel):
    field: str
    old: Any = None
    new: Any = None


class PlannedOp(BaseModel):
    hire_id: str
    system: Literal["okta", "lumos", "equipment", "tracker", "workday"]
    action: str
    fields: list[str]
    payload: dict[str, Any]
    idempotency_key: str


class OpResult(BaseModel):
    status: Literal["done", "failed_transient", "failed_permanent", "skipped_duplicate"]
    attempts: int = 0
    error: str | None = None
    system: str = ""
    action: str = ""
    idempotency_key: str = ""


class ValidationIssue(BaseModel):
    rule_id: str
    severity: Literal["block", "warn"]
    field: str
    message: str


class ExceptionDecision(BaseModel):
    """Claude's structured output. Validated by Pydantic before anything uses it."""

    decision: Literal["apply", "hold", "reject", "needs_human"]
    recommended_value: dict[str, Any] | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str
    slack_message: str


class ExceptionCase(BaseModel):
    case_id: str
    hire_id: str
    kind: str
    context: dict[str, Any] = Field(default_factory=dict)
    status: Literal["open", "auto_resolved", "awaiting_human", "approved", "rejected"] = "open"
    # Work that was withheld and runs only if the case is approved/auto-resolved.
    held_ops: list[PlannedOp] = Field(default_factory=list)
    held_fields: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] | None = None
