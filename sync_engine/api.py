"""HTTP front door (FastAPI). n8n/Zapier call this; the engine owns logic and state.

Security: /events verifies an HMAC-SHA256 of the raw body (X-Signature: hex) against SYNC_WEBHOOK_SECRET.
Production would use each vendor's own webhook signing scheme, OAuth client-credentials per target system, and a vault.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ValidationError

from .models import SourceEvent
from .pipeline import Pipeline

app = FastAPI(title="Onboarding sync engine")
_lock = threading.Lock()  # one sqlite connection shared across worker threads
_pipe: Pipeline | None = None


def pipeline() -> Pipeline:
    global _pipe
    if _pipe is None:
        _pipe = Pipeline(os.environ.get("SYNC_DATA_DIR", "data"))
    return _pipe


def _secret() -> bytes:
    # ASSUMPTION: a dev default keeps the local demo working; production must set a real secret.
    return os.environ.get("SYNC_WEBHOOK_SECRET", "dev-secret").encode()


def sign(body: bytes) -> str:
    return hmac.new(_secret(), body, hashlib.sha256).hexdigest()


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True, "claude_mode": pipeline().claude.mode}


@app.post("/events")
async def ingest(request: Request, x_signature: str = Header(default="")) -> dict[str, Any]:
    body = await request.body()
    if not hmac.compare_digest(x_signature, sign(body)):  # constant-time compare
        raise HTTPException(401, "bad signature")
    try:
        event = SourceEvent.model_validate(json.loads(body))
    except (ValueError, ValidationError) as e:
        raise HTTPException(422, f"invalid event: {str(e)[:200]}")
    with _lock:
        rep = pipeline().process(event)
    return {"outcome": rep.outcome, "hire_id": rep.hire_id,
            "ops": [{"system": r.system, "action": r.action, "status": r.status, "attempts": r.attempts} for r in rep.results],
            "exceptions": [{"case_id": c.case_id, "kind": c.kind, "status": c.status} for c in rep.cases],
            "issues": [i.model_dump() for i in rep.issues]}


@app.get("/hires/{hire_id}/status")
def hire_status(hire_id: str) -> dict[str, Any]:
    with _lock:
        p = pipeline()
        hire = p.store.get_hire(hire_id)
        if hire is None:
            raise HTTPException(404, "unknown hire")
        return {"hire": hire.model_dump(mode="json"),
                "ops": [{"system": r["system"], "action": r["action"], "status": r["status"], "attempts": r["attempts"]}
                        for r in p.store.ledger_list(hire_id=hire_id)],
                "open_exceptions": [c.model_dump(mode="json", include={"case_id", "kind", "status"})
                                    for c in p.store.list_exceptions("awaiting_human") if c.hire_id == hire_id]}


@app.get("/exceptions")
def exceptions(status: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        return [c.model_dump(mode="json", exclude={"context"}) for c in pipeline().store.list_exceptions(status)]


class Resolution(BaseModel):
    decision: str  # approve | reject


@app.post("/exceptions/{case_id}/resolve")
def resolve(case_id: str, body: Resolution) -> dict[str, Any]:
    if body.decision not in ("approve", "reject"):
        raise HTTPException(422, "decision must be approve or reject")
    with _lock:
        try:
            case = pipeline().resolve(case_id, body.decision)
        except KeyError:
            raise HTTPException(404, "unknown case")
    return {"case_id": case.case_id, "status": case.status}


# ---- dev-only demo dashboard (SYNC_DEV_UI=1) ---------------------------------------------
# A page so a non-engineer can click a scenario and watch systems change. Unsigned on purpose, so it is
# off by default; never enable it outside a local demo.
import shutil  # noqa: E402
from pathlib import Path  # noqa: E402

from fastapi.responses import HTMLResponse  # noqa: E402

_ROOT = Path(__file__).resolve().parent.parent


def _scenarios() -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((_ROOT / "mocks" / "scenarios").glob("S*.json"))]


if os.environ.get("SYNC_DEV_UI") == "1":
    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return (_ROOT / "sync_engine" / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/ui/scenarios")
    def ui_scenarios() -> list[dict[str, str]]:
        return [{"id": s["id"], "title": s["title"]} for s in _scenarios()]

    @app.get("/ui/state")
    def ui_state() -> dict[str, Any]:
        with _lock:
            p = pipeline()
            audit = p.store.conn.execute(
                "SELECT id, hire_id, event_id, stage, detail_json FROM audit_log ORDER BY id DESC LIMIT 60")
            return {
                "today": p.today.isoformat(), "claude_mode": p.claude.mode,
                "hires": [h.model_dump(mode="json") for h in p.store.all_hires()],
                "systems": {name: a.snapshot()["records"] for name, a in p.adapters.items()},
                "ledger": [dict(r) for r in p.store.ledger_list()],
                "exceptions": [c.model_dump(mode="json", exclude={"context", "held_fields"}) for c in p.store.list_exceptions()],
                "slack": [m["blocks"][0]["text"]["text"] for m in p.slack.messages()],
                "audit": [dict(r) for r in audit],
            }

    @app.post("/ui/run/{sid}")
    def ui_run(sid: str) -> list[dict[str, Any]]:
        sc = next((s for s in _scenarios() if s["id"] == sid), None)
        if sc is None:
            raise HTTPException(404, "unknown scenario")
        out = []
        with _lock:
            p = pipeline()
            setup = sc.get("setup", {})
            p.failures.configure(setup.get("failures"))
            for hid in setup.get("mark_shipped", []):
                p.equipment.mark_shipped(hid)
            for raw in sc["events"]:
                rep = p.process(SourceEvent.model_validate(raw))
                out.append({"event": rep.event_id, "outcome": rep.outcome, "hire": rep.hire_id,
                            "ops": [f"{r.system}:{r.status}" for r in rep.results],
                            "exceptions": [c.kind for c in rep.cases],
                            "warnings": [f"{i.rule_id} {i.message}" for i in rep.issues]})
            p.failures.configure(None)
        return out

    @app.post("/ui/reset")
    def ui_reset() -> dict[str, bool]:
        global _pipe
        with _lock:
            if _pipe is not None:
                _pipe.store.conn.close()
            shutil.rmtree(os.environ.get("SYNC_DATA_DIR", "data"), ignore_errors=True)
            _pipe = None
        return {"ok": True}
