# BetterUp onboarding sync (take-home prototype)

Propagates post-offer hire changes (and off-cycle Workday hires) to Okta, Lumos, Equipment and the Cohort Tracker,
with a validation gate, idempotent execution, an audit trail, and Claude as an exception handler. All systems are mocked.

## Quickstart
```bash
make setup     # pydantic, rich, pytest (anthropic optional)
make test      # 47 tests
make demo      # resets data/, runs S1-S8; works with or without ANTHROPIC_API_KEY
```
Operator CLI: `python -m sync_engine.cli audit H-1001 | exceptions | resolve <case> approve|reject | dead-letters | retry | ingest <file>`
(set `SYNC_DATA_DIR=data` after a demo run to inspect it; Slack messages land in `data/slack_outbox.jsonl`).

## Architecture
See [docs/systems_map.md](docs/systems_map.md). Pipeline: ingest/dedupe -> normalise -> stale check -> match identity ->
validate V1-V8 -> diff -> plan (propagation matrix) -> execute (ledger, retry, isolation) -> exceptions -> audit.

## Scenarios
S1 happy path (5 writes incl. Workday) · S2 start date moved (V2 warn) · S3 Ashby/Workday name mismatch (Claude -> Slack) · S4 Workday-only hire ·
S5 replay = 0 writes · S6 503 retry + Okta 409 + isolation · S7 address change after shipment · S8 stale event.

## Layout
`sync_engine/` (models, store, mappers, validation, differ, planner, executor, matcher, claude_handler, pipeline, cli, adapters/,
prompts/) · `mocks/` payload docs + scenarios · `tests/` · `docs/` · `BUILD_NOTE.md` · `AI_LOG.md`.

## Limitations / assumptions
Payload shapes and target attributes are invented (`# ASSUMPTION:` in code). No holiday calendar. Failure config
`config/failures.yaml` is JSON (valid YAML) to avoid a YAML dependency. Claude path was unit-tested with a fake client;
not exercised against the live API here. The n8n JSON is hand-written and has not been imported into a real n8n.

## See it visually
`SYNC_DEV_UI=1 SYNC_TODAY=2026-11-02 python -m uvicorn sync_engine.api:app --port 8000`, open http://localhost:8000/ and click
S1..S8 (dev-only dashboard, unsigned, local demo use only).

## Bonus items
- **API:** `SYNC_TODAY=2026-11-02 python -m uvicorn sync_engine.api:app --port 8000`, then open http://localhost:8000/docs
  and run `python demo_api.py` (signs each event with HMAC `X-Signature`). Endpoints: `POST /events`,
  `GET /hires/{id}/status`, `GET /exceptions`, `POST /exceptions/{id}/resolve`, `GET /health`.
- **MCP:** `mcp_server.py` (needs `pip install "mcp<2"`); registration steps are in its docstring.
- **n8n:** `n8n/onboarding_sync.json` (webhook -> POST /events; daily GET /exceptions -> Slack).
- **Deadlines:** seeded per new hire (`deadlines` table); `python -m sync_engine.cli check-deadlines`.
