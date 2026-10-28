"""Orchestrates one event through ingest -> normalise -> match -> validate -> diff -> plan -> execute -> exceptions.

Side effects live in adapters and the store; the stages themselves are pure-ish modules.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from . import differ, matcher, planner, validation
from .adapters.base import FailureConfig
from .adapters.equipment import EquipmentAdapter
from .adapters.lumos import LumosAdapter
from .adapters.okta import OktaAdapter
from .adapters.slack import SlackAdapter
from .adapters.tracker import TrackerAdapter
from .adapters.workday import WorkdayAdapter
from .claude_handler import ClaudeHandler, enforce_policy
from .executor import Executor
from .mappers import MappingError, source_key, to_hire
from .models import (ExceptionCase, ExceptionDecision, FieldChange, Hire, OpResult, PlannedOp,
                     SourceEvent, ValidationIssue)
from .store import Store

CLAUDE_KINDS = {"cross_source_mismatch", "identity_ambiguous", "post_shipment_address", "late_start_change"}


class SimulatedCrash(Exception):
    """Raised by the crash hook to prove replay safety in tests/demos."""


@dataclass
class RunReport:
    event_id: str
    outcome: str = "processed"  # processed | duplicate | stale_ignored | held_identity | bad_payload
    hire_id: str | None = None
    changes: list[FieldChange] = field(default_factory=list)
    issues: list[ValidationIssue] = field(default_factory=list)
    results: list[OpResult] = field(default_factory=list)
    cases: list[ExceptionCase] = field(default_factory=list)


class Pipeline:
    def __init__(self, data_dir: str | Path = "data", today: date | None = None, failures: FailureConfig | None = None,
                 sleep: Callable[[float], None] = time.sleep, claude: ClaudeHandler | None = None,
                 base_delay: float | None = None):
        self.data_dir = Path(data_dir)
        env_today = os.environ.get("SYNC_TODAY")
        self.today = today or (date.fromisoformat(env_today) if env_today else date.today())
        self.failures = failures or FailureConfig.load()
        self.store = Store(self.data_dir / "sync.db")
        self.okta = OktaAdapter(self.data_dir, self.failures)
        self.lumos = LumosAdapter(self.data_dir, self.failures)
        self.equipment = EquipmentAdapter(self.data_dir, self.failures)
        self.tracker = TrackerAdapter(self.data_dir, self.failures)
        self.workday = WorkdayAdapter(self.data_dir, self.failures)
        self.slack = SlackAdapter(self.data_dir)
        self.adapters = {a.name: a for a in (self.okta, self.lumos, self.equipment, self.tracker, self.workday)}
        delay = base_delay if base_delay is not None else float(os.environ.get("SYNC_BACKOFF_BASE", "0.5"))
        self.executor = Executor(self.store, self.adapters, sleep=sleep, base_delay=delay)  # type: ignore[arg-type]
        self.claude = claude or ClaudeHandler()
        self.crash_point: str | None = None

    def _crash(self, point: str) -> None:
        if self.crash_point == point:
            raise SimulatedCrash(point)

    # ---- entry point ---------------------------------------------------------
    def process(self, event: SourceEvent) -> RunReport:
        state = self.store.begin_event(event.event_id)
        if state == "duplicate":
            # Ordering/dedupe by id: a completed event is a no-op, but still audited.
            self.store.audit(None, event.event_id, "duplicate_event", {"source": event.source})
            return RunReport(event.event_id, "duplicate")
        if state == "reprocess":
            self.store.audit(None, event.event_id, "reprocess_after_crash", {})
        return self._run(event)

    def _run(self, event: SourceEvent, force_match: str | None = None) -> RunReport:
        rep = RunReport(event.event_id)
        eid = event.event_id
        self.store.audit(None, eid, "ingest", {"source": event.source, "type": event.event_type, "source_version": event.source_version})

        # normalise
        try:
            new = to_hire(event)
        except (MappingError, ValidationError) as e:
            rep.outcome = "bad_payload"
            self.store.audit(None, eid, "normalize_failed", {"error": str(e)[:300]})
            rep.cases.append(self._open_exception(eid, "bad_payload", "UNKNOWN", {"error": str(e)[:300]}, [], {}, set(),
                                                  summary=f"Could not normalise {event.source} payload."))
            self.store.finish_event(eid)
            return rep

        # staleness: compare source_version (never wall-clock) against the last one seen for this source entity
        key = source_key(new)
        rec = self.store.get_source_record(new.source, key)
        if rec and event.source_version <= rec["source_version"]:
            rep.outcome, rep.hire_id = "stale_ignored", rec["hire_id"]
            self.store.audit(rec["hire_id"], eid, "stale_event_ignored",
                             {"event_version": event.source_version, "stored_version": rec["source_version"]})
            self.store.finish_event(eid)
            return rep

        # identity
        m = matcher.match(self.store, new, force_match)
        if m.kind == "ambiguous":
            rep.outcome, rep.hire_id = "held_identity", m.hire_id
            ctx = self._identity_context(new, self.store.get_hire(m.hire_id or ""))
            ctx["_event"] = event.model_dump(mode="json")  # kept so approve/reject can resume this exact event
            rep.cases.append(self._open_exception(eid, "identity_ambiguous", m.hire_id or "UNKNOWN", ctx, [], {}, set(),
                                                  summary=m.reason))
            self.store.audit(m.hire_id, eid, "match", {"result": "ambiguous", "reason": m.reason})
            self.store.finish_event(eid)
            return rep
        old = self.store.get_hire(m.hire_id) if m.kind == "existing" and m.hire_id else None
        hire_id = m.hire_id if (m.kind == "existing" and m.hire_id) else self.store.next_hire_id()
        # Merge the other source's id so one Hire carries both identities.
        carry = {"hire_id": hire_id}
        if old:
            carry["ashby_candidate_id"] = new.ashby_candidate_id or old.ashby_candidate_id
            carry["workday_worker_id"] = new.workday_worker_id or old.workday_worker_id
        new = new.model_copy(update=carry)
        rep.hire_id = hire_id
        self.store.audit(hire_id, eid, "match", {"result": m.kind, "reason": m.reason})

        # validate + diff
        other = self.store.other_source_record(hire_id, new.source)
        changes = differ.diff(old, new)
        changed_groups = {differ.group_of(c.field) for c in changes}
        issues = [i for i in validation.validate_all(new, old, other, self.today)
                  if i.rule_id != "V7" or old is None or i.field in changed_groups]
        rep.changes, rep.issues = changes, issues
        self.store.audit(hire_id, eid, "validate", {"issues": [i.model_dump() for i in issues]})
        self.store.audit(hire_id, eid, "diff", {"fields": [c.field for c in changes]})
        block_issues = [i for i in issues if i.severity == "block"]
        blocked = {differ.group_of(i.field) for i in block_issues}

        # plan
        facts = planner.Facts(shipped=self.equipment.is_shipped(hire_id), okta_active=self.okta.is_active(hire_id))
        pr = planner.plan(old, new, changes, facts, blocked) if (old is None or changes) else planner.PlanResult()
        self.store.audit(hire_id, eid, "plan", {"ops": [f"{o.system}.{o.action}" for o in pr.ops],
                                                 "held": [f"{o.system}.{o.action}" for o in pr.held_ops]})

        # execute (isolated per system) then open exceptions for failures
        rep.results = self.executor.run(pr.ops, eid)
        self._crash("after_execute")
        failed = {r.idempotency_key: r for r in rep.results if r.status == "failed_permanent"}
        for op in pr.ops:
            if op.idempotency_key in failed:
                rep.cases.append(self._open_exception(
                    eid, "op_failed_permanent", hire_id, {"system": op.system, "action": op.action, "error": failed[op.idempotency_key].error},
                    [op], {}, set(), summary=f"{op.system}.{op.action} failed permanently: {failed[op.idempotency_key].error}"))

        v7 = [i for i in block_issues if i.rule_id == "V7"]
        other_blocks = [i for i in block_issues if i.rule_id != "V7"]
        if v7:
            ctx = {"mismatches": [i.message for i in v7], "fields": sorted({i.field for i in v7}),
                   "sources": [new.source, other.source if other else None]}
            rep.cases.append(self._open_exception(eid, "cross_source_mismatch", hire_id, ctx, pr.held_ops,
                                                  self._held_fields(new, changes, {differ.group_of(i.field) for i in v7}),
                                                  {i.field for i in v7}, summary="; ".join(i.message for i in v7)))
        if other_blocks:
            fields = {i.field for i in other_blocks}
            held = pr.held_ops  # ledger keys make a double-run of overlapping held ops harmless
            rep.cases.append(self._open_exception(eid, "validation_block", hire_id, {"rules": [f"{i.rule_id}:{i.message}" for i in other_blocks]},
                                                  held, self._held_fields(new, changes, {differ.group_of(f) for f in fields}),
                                                  fields, summary="; ".join(i.message for i in other_blocks)))
        for pe in pr.exceptions:
            ctx = dict(pe.context)
            rep.cases.append(self._open_exception(eid, pe.kind, hire_id, ctx, pe.held_ops,
                                                  {k: (v.model_dump(mode="json") if hasattr(v, "model_dump") else v) for k, v in pe.held_fields.items()},
                                                  set(pe.fields), summary=pe.kind.replace("_", " ")))

        # commit: applied state excludes withheld fields so the next diff re-detects them
        withheld = blocked | pr.deferred_groups
        applied = self._apply(old, new, [c for c in changes if differ.group_of(c.field) not in withheld]) if old else new
        with self.store.tx():
            if old is None:
                self._seed_deadlines(new)
            self.store.put_hire(applied)
            self.store.put_source_record(new, key)
            self.store.finish_event(eid)
        self.store.audit(hire_id, eid, "commit", {"source_version": new.source_version, "withheld": sorted(withheld)})
        if not changes and old:
            rep.outcome = "no_changes"
        return rep

    def _seed_deadlines(self, h: Hire) -> None:
        """ASSUMPTION: due dates are illustrative offsets from start_date."""
        from datetime import timedelta
        self.store.put_deadline(h.hire_id, "it_form_due", planner.ship_by(h.start_date).isoformat())
        self.store.put_deadline(h.hire_id, "bg_check_due", (h.start_date - timedelta(days=7)).isoformat())
        self.store.put_deadline(h.hire_id, "okta_provision_due", (h.start_date - timedelta(days=2)).isoformat())

    def check_deadlines(self, at_risk_days: int = 2) -> list[dict[str, str]]:
        """Post overdue / at-risk items to Slack. Completion is read from the ops ledger, not re-tracked."""
        system_for = {"it_form_due": "equipment", "okta_provision_due": "okta"}
        out = []
        for d in self.store.list_deadlines():
            if d["status"] == "done":
                continue
            sys_name = system_for.get(d["kind"])
            if sys_name and any(r["system"] == sys_name and r["status"] == "done" for r in self.store.ledger_list(hire_id=d["hire_id"])):
                self.store.set_deadline_status(d["hire_id"], d["kind"], "done")
                continue
            days = (date.fromisoformat(d["due_date"]) - self.today).days
            state = "overdue" if days < 0 else "at_risk" if days <= at_risk_days else None
            if state:
                item = {"hire_id": d["hire_id"], "kind": d["kind"], "due": d["due_date"], "state": state}
                self.slack.post_exception(f"D-{d['hire_id']}-{d['kind']}", f"deadline_{state}", d["hire_id"],
                                          f"{d['kind']} due {d['due_date']} ({state})", needs_approval=False)
                self.store.audit(d["hire_id"], None, f"deadline_{state}", item)
                out.append(item)
        return out

    # ---- helpers -------------------------------------------------------------
    @staticmethod
    def _apply(old: Hire, new: Hire, allowed: list[FieldChange]) -> Hire:
        upd: dict[str, Any] = {c.field: getattr(new, c.field) for c in allowed}
        upd.update(ashby_candidate_id=new.ashby_candidate_id, workday_worker_id=new.workday_worker_id,
                   source=new.source, source_version=new.source_version, updated_at=new.updated_at)
        return old.model_copy(update=upd)

    @staticmethod
    def _held_fields(new: Hire, changes: list[FieldChange], groups: set[str]) -> dict[str, Any]:
        return {c.field: c.new for c in changes if differ.group_of(c.field) in groups}

    @staticmethod
    def _identity_context(new: Hire, existing: Hire | None) -> dict[str, Any]:
        """Minimal data: names and whether emails match, never addresses."""
        return {"incoming_name": f"{new.legal_first_name} {new.legal_last_name}", "incoming_source": new.source,
                "existing_name": f"{existing.legal_first_name} {existing.legal_last_name}" if existing else None,
                "same_start_date": bool(existing and existing.start_date == new.start_date),
                "same_email": bool(existing and existing.personal_email.lower() == new.personal_email.lower())}

    def _open_exception(self, event_id: str, kind: str, hire_id: str, context: dict[str, Any], held_ops: list[PlannedOp],
                        held_fields: dict[str, Any], fields: set[str], summary: str) -> ExceptionCase:
        # Deterministic id => replaying a crashed event cannot create duplicate cases.
        case_id = "C-" + hashlib.sha256(f"{event_id}|{kind}|{hire_id}".encode()).hexdigest()[:8]
        existing = self.store.get_exception(case_id)
        if existing:
            return existing
        case = ExceptionCase(case_id=case_id, hire_id=hire_id, kind=kind, context=context, held_ops=held_ops,
                             held_fields=held_fields, status="open")
        decision: ExceptionDecision | None = None
        if kind in CLAUDE_KINDS:
            safe_ctx = {k: v for k, v in context.items() if not k.startswith("_")}
            res = self.claude.decide(kind, hire_id, safe_ctx, summary)
            decision = res.decision
            case.decision = {**decision.model_dump(), "mode": res.mode, "model": res.model, "prompt_hash": res.prompt_hash}
            self.store.audit(hire_id, event_id, "claude_decision", {
                "mode": res.mode, "model": res.model, "prompt_hash": res.prompt_hash, "decision": decision.decision,
                "confidence": decision.confidence, "error": res.error})
            auto, why = enforce_policy(decision, kind, fields)
            self.store.audit(hire_id, event_id, "policy", {"auto_apply": auto, "reason": why})
            if auto:
                case.status = "auto_resolved"
                self._execute_held(case, event_id)
        if case.status != "auto_resolved":
            case.status = "awaiting_human"
            text = decision.slack_message if decision else f"{summary}. Needs human review."
            if decision:
                text += f"\n_Claude ({case.decision['mode']}): {decision.decision}, confidence {decision.confidence:.2f} — {decision.reasoning}_"
            self.slack.post_exception(case_id, kind, hire_id, text, needs_approval=True)
        self.store.put_exception(case)
        self.store.audit(hire_id, event_id, "exception", {"case_id": case_id, "kind": kind, "status": case.status})
        return case

    def _execute_held(self, case: ExceptionCase, event_id: str | None) -> list[OpResult]:
        results = self.executor.run(case.held_ops, event_id, force=True)
        if case.held_fields:
            hire = self.store.get_hire(case.hire_id)
            if hire:
                data = hire.model_dump()
                data.update(case.held_fields)
                self.store.put_hire(Hire.model_validate(data))
        return results

    # ---- human decisions -----------------------------------------------------
    def resolve(self, case_id: str, decision: str) -> ExceptionCase:
        """Simulates a human clicking Approve/Reject in Slack. Safe to call twice (second call is a no-op)."""
        case = self.store.get_exception(case_id)
        if case is None:
            raise KeyError(case_id)
        if case.status != "awaiting_human":
            return case
        self.store.audit(case.hire_id, None, "human_decision", {"case_id": case_id, "decision": decision})
        if case.kind == "identity_ambiguous":
            ev = SourceEvent.model_validate(case.context["_event"])
            self._run(ev, force_match=case.hire_id if decision == "approve" else "new")
        elif decision == "approve":
            self._execute_held(case, None)  # idempotent: ledger skips anything already done
        case.status = "approved" if decision == "approve" else "rejected"
        self.store.put_exception(case)
        return case

    def retry_dead_letters(self) -> list[OpResult]:
        out = []
        for row in self.store.ledger_list("failed_transient"):
            op = PlannedOp(hire_id=row["hire_id"], system=row["system"], action=row["action"], fields=[],
                           payload=json.loads(row["payload_json"]), idempotency_key=row["idempotency_key"])
            out.append(self.executor.run_one(op, None, force=True))
        return out
