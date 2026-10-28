# AI_LOG — where generated code was wrong and got corrected

Real entries only. Dates are when found.

## 2026-10-28 — Approved post-shipment address change could never execute
- **What was wrong:** the equipment mock refuses address writes once `shipped=True` (defence in depth), and the
  planner's *held* op for an approved post-shipment address change used the same plain `update_request`. So when a
  human clicked Approve, the op hit the adapter's own 409 guard and silently stayed unapplied.
- **How caught:** `test_address_change_after_shipment_is_exception_without_equipment_write` — after `resolve(approve)` the
  equipment address was still the old one.
- **Fix:** the held op carries `reship_approved=True` (only ever built for the post-approval path); the adapter accepts
  a shipped-package address change only with that flag. Planner still never emits the write on its own.

## 2026-10-28 — Per-hire audit trail dropped the first stages of every event
- **What was wrong:** `audit_for(hire_id)` filtered on `hire_id`, but `ingest`, `duplicate_event` and `stale_event_ignored`
  rows are written before identity is resolved (hire_id NULL), so the trail for H-1001 started at `match` and the replay
  in S5 was invisible.
- **How caught:** reading the `make demo` output for H-1001.
- **Fix:** `audit_for` also returns rows sharing an `event_id` with any row of that hire.

## 2026-10-28 — Test assumed a same-name hire would create a second Okta login (test was wrong, code was right)
- **What was wrong:** my login-collision test processed "Alex Rivera" twice with different emails and expected
  `alex.rivera2`. The matcher (correctly) held the second as `identity_ambiguous` instead of guessing.
- **How caught:** the test failed with one Okta record.
- **Fix:** test now follows the real path: human rejects the match ("different person") -> new hire -> collision digit.
