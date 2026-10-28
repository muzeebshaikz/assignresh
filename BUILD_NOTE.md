# Build note

## Why this slice
A validation gate alone catches bad data but nothing moves it; monitoring alone reports lateness but cannot fix it.
Change propagation with a built-in validation gate is the point where both meet: every downstream system depends on the
data being right *and* delivered. The same SQLite state (hires, ops ledger, audit log) is what deadline monitoring would
read (overdue = ledger op not `done` past its due date), and the validation rules are already a pipeline stage. So the
other two slices plug into this store rather than needing their own.

## Key design decisions
- **Canonical `Hire`.** Ashby and Workday payloads are normalised once; every later stage is source-agnostic.
- **Ordering by `source_version`, never timestamps.** Clocks across vendors disagree and webhooks arrive out of order.
  Versions are tracked per (source, source entity) because Ashby and Workday counters are independent.
- **Two layers of idempotency.** Event dedupe (`events_seen`) stops replays cheaply; op keys
  `sha256(hire|system|action|payload)` make each write safe on their own. An event is marked `done` only in the same
  transaction as the hire state, so a crash mid-run leaves it `processing` and the replay resumes via the ledger
  (`test_crash_after_execute_then_replay_skips_done_ops`). Payloads include `as_of=source:version`, otherwise a revert
  A→B→A would reuse a `done` key and be silently skipped.
- **Transient vs permanent.** Transient: 3 attempts, exponential backoff + jitter, then dead-letter (`cli retry`).
  Permanent: no retry, exception + Slack. Unexpected exceptions are classified permanent and audited, never swallowed.
- **Per-system isolation.** One failing system never blocks the others; partial success is recorded (S6).
- **Workday is both source and destination.** Ashby-originated data is written into Workday (the HR record) after validation; a change is never written back to the system it came from, which also prevents echo loops.
- **Blocking is per field.** A bad address holds only the equipment op; the other systems still get the hire.
- **Rules first, Claude second, policy in code.** Claude only triages V7 mismatches, ambiguous identity and post-shipment
  changes, with forced tool-call output validated by Pydantic. Identity fields are never auto-applied, and I additionally
  require a human for post-shipment/identity cases regardless of confidence (stricter than the spec's 0.85 rule, since
  those are irreversible). No API key, API error or bad output -> `needs_human`.

## What I chose not to add
No queue, workflow engine or new database. n8n already runs on Postgres and can call an HTTP endpoint; the missing piece
was logic and state, not another orchestrator. SQLite stands in for that Postgres.

## What I would productionise
Signed webhooks (vendor signing) and OAuth service accounts per target, secrets in a vault; Postgres with row locking
per hire; metrics and alerting on dead-letters and exception age; Slack interactive approvals; a holiday calendar for
`ship_by`; deadline monitoring; PII retention (audit stores minimal detail, but names/emails live in `hires`).
Bonus items built: FastAPI, n8n export (untested import), MCP server, deadline stub, dev dashboard.

## AI tools used
Claude Code scaffolded modules and tests; Claude API is a runtime component (exception handler only).
**One thing the AI got wrong:** the approved post-shipment address change could never execute, because the generated
planner reused the plain update op that the equipment adapter's own "already shipped" guard rejects. A test caught it;
the fix was an explicit `reship_approved` flag that only the post-approval op carries. (AI_LOG.md has the full entry.)
