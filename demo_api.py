"""Posts the scenario events to a running API (default http://localhost:8000), signed like a webhook sender would.

Usage: python demo_api.py [S1 S2 ...]   (default: all)
Server: SYNC_TODAY=2026-11-02 python -m uvicorn sync_engine.api:app --port 8000
"""
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

import httpx

BASE = os.environ.get("SYNC_API", "http://localhost:8000")
SECRET = os.environ.get("SYNC_WEBHOOK_SECRET", "dev-secret").encode()

wanted = set(sys.argv[1:])
for path in sorted(Path("mocks/scenarios").glob("S*.json")):
    sc = json.loads(path.read_text())
    if wanted and sc["id"] not in wanted:
        continue
    for ev in sc["events"]:
        body = json.dumps(ev).encode()
        sig = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        r = httpx.post(f"{BASE}/events", content=body, headers={"X-Signature": sig}, timeout=30)
        d = r.json()
        print(f"{sc['id']} {ev['event_id']}: HTTP {r.status_code} outcome={d.get('outcome')} hire={d.get('hire_id')} "
              f"ops={[o['system'] + ':' + o['status'] for o in d.get('ops', [])]} exceptions={[e['kind'] for e in d.get('exceptions', [])]}")
