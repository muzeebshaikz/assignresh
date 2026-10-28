import sqlite3

import pytest

from sync_engine.pipeline import SimulatedCrash
from tests.factories import ADDR, ashby, ev, new_alex, workday


def writes(p):
    return sum(a.writes() for a in p.adapters.values())


# ---- idempotency / ordering ---------------------------------------------------
def test_replayed_event_produces_zero_new_writes(pipe):
    pipe.process(new_alex())
    before, done = writes(pipe), len(pipe.store.ledger_list("done"))
    rep = pipe.process(new_alex())
    assert rep.outcome == "duplicate"
    assert writes(pipe) == before and len(pipe.store.ledger_list("done")) == done
    assert any(r["stage"] == "duplicate_event" for r in pipe.store.audit_for("H-1001"))


def test_crash_after_execute_then_replay_skips_done_ops(pipe):
    pipe.crash_point = "after_execute"
    with pytest.raises(SimulatedCrash):
        pipe.process(new_alex())
    n = writes(pipe)
    assert n == 5  # writes happened, event not committed
    pipe.crash_point = None
    rep = pipe.process(new_alex())  # same event id -> 'reprocess'
    assert {r.status for r in rep.results} == {"skipped_duplicate"}
    assert writes(pipe) == n
    assert len(pipe.okta.snapshot()["records"]) == 1


def test_stale_source_version_ignored(pipe):
    pipe.process(new_alex())
    pipe.process(new_alex("e2", 2, "2026-11-09"))
    n = writes(pipe)
    rep = pipe.process(new_alex("e3", 2, "2026-11-23"))
    assert rep.outcome == "stale_ignored" and writes(pipe) == n
    assert pipe.store.get_hire("H-1001").start_date.isoformat() == "2026-11-09"


def test_revert_a_b_a_is_not_swallowed_by_idempotency(pipe):
    pipe.process(new_alex())
    pipe.process(new_alex("e2", 2, "2026-11-23"))
    pipe.process(new_alex("e3", 3, "2026-11-16"))
    assert pipe.tracker.snapshot()["records"]["H-1001"]["start_date"] == "2026-11-16"


def test_unchanged_record_yields_no_ops(pipe):
    pipe.process(new_alex())
    rep = pipe.process(new_alex("e2", 2))
    assert rep.outcome == "no_changes" and rep.results == []


# ---- failure handling -----------------------------------------------------------
def test_transient_retried_then_succeeds(pipe):
    pipe.failures.configure({"equipment": {"fail_first_calls": 2, "status": 503}})
    rep = pipe.process(new_alex())
    eq = next(r for r in rep.results if r.system == "equipment")
    assert eq.status == "done" and eq.attempts == 3


def test_transient_exhausted_is_dead_lettered_and_resumable(pipe):
    pipe.failures.configure({"equipment": {"fail_first_calls": 5}})
    rep = pipe.process(new_alex())
    assert next(r for r in rep.results if r.system == "equipment").status == "failed_transient"
    assert len(pipe.store.ledger_list("failed_transient")) == 1
    pipe.failures.configure(None)
    assert [r.status for r in pipe.retry_dead_letters()] == ["done"]
    assert pipe.store.ledger_list("failed_transient") == []


def test_permanent_not_retried_and_isolated(pipe):
    pipe.failures.configure({"okta": {"conflict_logins": ["alex.rivera@betterup.co"]}})
    rep = pipe.process(new_alex())
    by = {r.system: r for r in rep.results}
    assert by["okta"].status == "failed_permanent" and by["okta"].attempts == 1
    assert all(by[s].status == "done" for s in ("lumos", "equipment", "tracker"))
    assert [c.kind for c in rep.cases] == ["op_failed_permanent"]


def test_okta_login_collision_appends_digit(pipe):
    pipe.process(new_alex())
    # same name, different email: matcher refuses to guess; human says "different person" -> new hire, login collides
    rep = pipe.process(ev(ashby("e9", 1, "cand-2", "Alex", "Rivera", "other@example.com", "2026-11-16", kind="hire.created")))
    assert rep.outcome == "held_identity"
    pipe.resolve(rep.cases[0].case_id, "reject")
    logins = sorted(r["login"] for r in pipe.okta.snapshot()["records"].values())
    assert logins == ["alex.rivera2@betterup.co", "alex.rivera@betterup.co"]


# ---- exceptions / policy ----------------------------------------------------------
def test_address_change_after_shipment_is_exception_without_equipment_write(pipe):
    pipe.process(new_alex())
    pipe.equipment.mark_shipped("H-1001")
    eq_before = pipe.equipment.snapshot()["records"]["H-1001"]["ship_address"]
    rep = pipe.process(new_alex("e2", 2, addr={**ADDR, "line1": "9 New St", "postalCode": "78702"}))
    assert [r.system for r in rep.results] == ["workday"] and rep.cases[0].kind == "post_shipment_address"
    assert rep.cases[0].status == "awaiting_human"
    assert pipe.equipment.snapshot()["records"]["H-1001"]["ship_address"] == eq_before
    # human approves -> held op executes exactly once, even if clicked twice
    pipe.resolve(rep.cases[0].case_id, "approve")
    pipe.resolve(rep.cases[0].case_id, "approve")
    assert pipe.equipment.snapshot()["records"]["H-1001"]["ship_address"]["line1"] == "9 New St"
    assert pipe.store.get_hire("H-1001").ship_address.line1 == "9 New St"


def test_reject_cancels_held_ops(pipe):
    pipe.process(new_alex())
    pipe.equipment.mark_shipped("H-1001")
    rep = pipe.process(new_alex("e2", 2, addr={**ADDR, "line1": "9 New St"}))
    pipe.resolve(rep.cases[0].case_id, "reject")
    assert pipe.equipment.snapshot()["records"]["H-1001"]["ship_address"]["line1"] == ADDR["line1"]
    assert pipe.store.get_exception(rep.cases[0].case_id).status == "rejected"


def test_cross_source_name_mismatch_blocks_names_only(pipe):
    pipe.process(ev(ashby("a1", 1, "c1", "Jon", "Smith", "jon@example.com", "2026-11-23", kind="hire.created")))
    rep = pipe.process(ev(workday("w1", 1, "WD1", "Jonathan", "Smyth", "jon@example.com", "2026-11-23", kind="hire.created")))
    assert rep.cases[0].kind == "cross_source_mismatch" and rep.cases[0].status == "awaiting_human"
    assert pipe.store.get_hire("H-1001").legal_first_name == "Jon"  # identity not overwritten
    assert pipe.okta.snapshot()["records"]["H-1001"]["first_name"] == "Jon"
    pipe.resolve(rep.cases[0].case_id, "approve")
    assert pipe.okta.snapshot()["records"]["H-1001"]["first_name"] == "Jonathan"


def test_legal_name_change_on_active_okta_account_never_automatic(pipe):
    pipe.process(new_alex())
    pipe.okta.mark_active("H-1001")
    login = pipe.okta.snapshot()["records"]["H-1001"]["login"]
    rep = pipe.process(ev(ashby("e2", 2, "cand-alex", "Alexandra", "Rivera", "alex.rivera@example.com", "2026-11-16")))
    assert rep.cases[0].kind == "legal_name_active_account"
    assert pipe.okta.snapshot()["records"]["H-1001"]["login"] == login
    assert pipe.tracker.snapshot()["records"]["H-1001"]["name"] == "Alex Rivera"


def test_name_change_before_activation_updates_login(pipe):
    pipe.process(new_alex())
    pipe.process(ev(ashby("e2", 2, "cand-alex", "Alexis", "Rivera", "alex.rivera@example.com", "2026-11-16")))
    assert pipe.okta.snapshot()["records"]["H-1001"]["login"] == "alexis.rivera@betterup.co"


def test_validation_block_holds_only_affected_systems(pipe):
    bad = ashby("e1", 1, "cand-x", "Alex", "Rivera", "alex.rivera@example.com", "2026-11-16", kind="hire.created",
                addr={**ADDR, "postalCode": "ABC"})
    rep = pipe.process(ev(bad))
    assert {r.system for r in rep.results} == {"okta", "lumos", "tracker"}  # equipment + workday creates held (V4)
    assert rep.cases[0].kind == "validation_block" and {o.system for o in rep.cases[0].held_ops} == {"equipment", "workday"}


def test_ambiguous_identity_held_then_resolved(pipe):
    pipe.process(new_alex())
    other = ashby("e5", 1, "cand-alex2", "Alex", "Riviera", "different@example.com", "2026-11-23", kind="hire.created")
    rep = pipe.process(ev(other))
    assert rep.outcome == "held_identity" and rep.cases[0].kind == "identity_ambiguous"
    assert len(pipe.store.all_hires()) == 1
    pipe.resolve(rep.cases[0].case_id, "reject")  # different person -> new hire
    assert len(pipe.store.all_hires()) == 2


def test_bad_payload_is_audited_exception(pipe):
    raw = ashby("bad", 1, "c", "A", "B", "a@b.co", "2026-11-16", kind="hire.created")
    del raw["payload"]["offer"]
    rep = pipe.process(ev(raw))
    assert rep.outcome == "bad_payload" and rep.cases[0].kind == "bad_payload"


def test_ashby_hire_creates_workday_prehire_and_update_follows(pipe):
    pipe.process(new_alex())
    assert pipe.workday.snapshot()["records"]["H-1001"]["start_date"] == "2026-11-16"
    pipe.process(new_alex("e2", 2, "2026-11-23"))
    assert pipe.workday.snapshot()["records"]["H-1001"]["start_date"] == "2026-11-23"


def test_workday_failure_is_isolated(pipe):
    pipe.failures.configure({"workday": {"fail_first_calls": 9}})
    rep = pipe.process(new_alex())
    by = {r.system: r.status for r in rep.results}
    assert by["workday"] == "failed_transient" and all(by[s] == "done" for s in ("okta", "lumos", "equipment", "tracker"))


def test_audit_log_is_append_only(pipe):
    pipe.process(new_alex())
    with pytest.raises(sqlite3.DatabaseError):
        pipe.store.conn.execute("DELETE FROM audit_log")
