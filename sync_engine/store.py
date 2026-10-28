"""SQLite persistence (stand-in for the Postgres n8n already runs on).

Why a store module at all: idempotency, ordering and the audit trail are all
state problems, so they live in one place that every stage shares.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import ExceptionCase, Hire, PlannedOp

SCHEMA = """
CREATE TABLE IF NOT EXISTS events_seen(
  event_id TEXT PRIMARY KEY, status TEXT NOT NULL, first_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS hires(
  hire_id TEXT PRIMARY KEY, json TEXT NOT NULL, source_version INTEGER NOT NULL);
-- Latest canonical record per (source, source entity). Source versions are
-- independent counters per source system, so ordering is tracked per source.
CREATE TABLE IF NOT EXISTS source_records(
  source TEXT NOT NULL, source_key TEXT NOT NULL, hire_id TEXT NOT NULL,
  source_version INTEGER NOT NULL, json TEXT NOT NULL,
  PRIMARY KEY(source, source_key));
CREATE TABLE IF NOT EXISTS ops_ledger(
  idempotency_key TEXT PRIMARY KEY, hire_id TEXT NOT NULL, system TEXT NOT NULL,
  action TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS exceptions(
  case_id TEXT PRIMARY KEY, hire_id TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL, json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, hire_id TEXT,
  event_id TEXT, stage TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deadlines(
  hire_id TEXT NOT NULL, kind TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  PRIMARY KEY(hire_id, kind));
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def canonical_json(obj: Any) -> str:
    """Stable serialisation so equal payloads always hash equally."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def make_idempotency_key(hire_id: str, system: str, action: str, payload: dict[str, Any]) -> str:
    raw = "|".join([hire_id, system, action, canonical_json(payload)])
    return hashlib.sha256(raw.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit; transactions are explicit via tx() so commit steps are atomic
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)  # API serialises access with a lock
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ---- events -------------------------------------------------------------
    def begin_event(self, event_id: str) -> str:
        """Return 'new', 'duplicate' (already completed) or 'reprocess' (crashed mid-run).

        An event is only 'done' after its effects are committed, so a crash
        leaves it 'processing' and the replay safely resumes via the ops ledger.
        """
        row = self.conn.execute("SELECT status FROM events_seen WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO events_seen VALUES(?,?,?)", (event_id, "processing", _now()))
            return "new"
        return "duplicate" if row["status"] == "done" else "reprocess"

    def finish_event(self, event_id: str) -> None:
        self.conn.execute("UPDATE events_seen SET status='done' WHERE event_id=?", (event_id,))

    # ---- hires / source records --------------------------------------------
    def get_hire(self, hire_id: str) -> Hire | None:
        row = self.conn.execute("SELECT json FROM hires WHERE hire_id=?", (hire_id,)).fetchone()
        return Hire.model_validate_json(row["json"]) if row else None

    def put_hire(self, hire: Hire) -> None:
        self.conn.execute(
            "INSERT INTO hires VALUES(?,?,?) ON CONFLICT(hire_id) DO UPDATE SET json=excluded.json, source_version=excluded.source_version",
            (hire.hire_id, hire.model_dump_json(), hire.source_version),
        )

    def all_hires(self) -> list[Hire]:
        rows = self.conn.execute("SELECT json FROM hires ORDER BY hire_id").fetchall()
        return [Hire.model_validate_json(r["json"]) for r in rows]

    def next_hire_id(self) -> str:
        row = self.conn.execute("SELECT MAX(CAST(SUBSTR(hire_id, 3) AS INTEGER)) m FROM hires").fetchone()
        return f"H-{(row['m'] or 1000) + 1}"

    def get_source_record(self, source: str, key: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM source_records WHERE source=? AND source_key=?", (source, key)
        ).fetchone()

    def put_source_record(self, hire: Hire, key: str) -> None:
        self.conn.execute(
            "INSERT INTO source_records VALUES(?,?,?,?,?) ON CONFLICT(source, source_key) DO UPDATE SET "
            "hire_id=excluded.hire_id, source_version=excluded.source_version, json=excluded.json",
            (hire.source, key, hire.hire_id, hire.source_version, hire.model_dump_json()),
        )

    def other_source_record(self, hire_id: str, source: str) -> Hire | None:
        row = self.conn.execute(
            "SELECT json FROM source_records WHERE hire_id=? AND source<>?", (hire_id, source)
        ).fetchone()
        return Hire.model_validate_json(row["json"]) if row else None

    # ---- ops ledger ---------------------------------------------------------
    def ledger_get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM ops_ledger WHERE idempotency_key=?", (key,)).fetchone()

    def ledger_set(self, op: PlannedOp, status: str, attempts: int, error: str | None) -> None:
        self.conn.execute(
            "INSERT INTO ops_ledger VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(idempotency_key) DO UPDATE SET "
            "status=excluded.status, attempts=excluded.attempts, last_error=excluded.last_error, updated_at=excluded.updated_at",
            (op.idempotency_key, op.hire_id, op.system, op.action, canonical_json(op.payload),
             status, attempts, error, _now()),
        )

    def ledger_list(self, status: str | None = None, hire_id: str | None = None) -> list[sqlite3.Row]:
        q, args = "SELECT * FROM ops_ledger WHERE 1=1", []
        if status:
            q += " AND status=?"; args.append(status)
        if hire_id:
            q += " AND hire_id=?"; args.append(hire_id)
        return self.conn.execute(q + " ORDER BY updated_at, rowid", args).fetchall()

    # ---- exceptions ---------------------------------------------------------
    def put_exception(self, case: ExceptionCase) -> None:
        self.conn.execute(
            "INSERT INTO exceptions VALUES(?,?,?,?,?,?) ON CONFLICT(case_id) DO UPDATE SET status=excluded.status, json=excluded.json",
            (case.case_id, case.hire_id, case.kind, case.status, case.model_dump_json(), _now()),
        )

    def get_exception(self, case_id: str) -> ExceptionCase | None:
        row = self.conn.execute("SELECT json FROM exceptions WHERE case_id=?", (case_id,)).fetchone()
        return ExceptionCase.model_validate_json(row["json"]) if row else None

    def list_exceptions(self, status: str | None = None) -> list[ExceptionCase]:
        q, args = "SELECT json FROM exceptions", []
        if status:
            q += " WHERE status=?"; args.append(status)
        return [ExceptionCase.model_validate_json(r["json"]) for r in self.conn.execute(q + " ORDER BY created_at, rowid", args)]

    # ---- deadlines (bonus: the monitoring slice reuses this store) ----------
    def put_deadline(self, hire_id: str, kind: str, due: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO deadlines(hire_id, kind, due_date) VALUES(?,?,?)", (hire_id, kind, due))

    def list_deadlines(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM deadlines ORDER BY due_date, hire_id").fetchall()

    def set_deadline_status(self, hire_id: str, kind: str, status: str) -> None:
        self.conn.execute("UPDATE deadlines SET status=? WHERE hire_id=? AND kind=?", (status, hire_id, kind))

    # ---- audit --------------------------------------------------------------
    def audit(self, hire_id: str | None, event_id: str | None, stage: str, detail: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(ts, hire_id, event_id, stage, detail_json) VALUES(?,?,?,?,?)",
            (_now(), hire_id, event_id, stage, canonical_json(detail or {})),
        )

    def audit_for(self, hire_id: str) -> list[sqlite3.Row]:
        # include pre-match rows (ingest, duplicate_event, stale) that share an event with this hire
        return self.conn.execute(
            "SELECT * FROM audit_log WHERE hire_id=? OR event_id IN "
            "(SELECT event_id FROM audit_log WHERE hire_id=? AND event_id IS NOT NULL) ORDER BY id",
            (hire_id, hire_id)).fetchall()
