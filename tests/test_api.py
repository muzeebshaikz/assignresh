import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from sync_engine import api
from sync_engine.adapters.base import FailureConfig
from sync_engine.pipeline import Pipeline
from tests.factories import ADDR, new_alex


@pytest.fixture
def client(tmp_path):
    api._pipe = Pipeline(tmp_path, today=date(2026, 11, 2), failures=FailureConfig(), sleep=lambda s: None, base_delay=0)
    yield TestClient(api.app)
    api._pipe = None


def post(client, ev, sig=None):
    body = json.dumps(ev.model_dump(mode="json")).encode()
    return client.post("/events", content=body, headers={"X-Signature": sig if sig is not None else api.sign(body)})


def test_bad_signature_rejected_and_nothing_written(client):
    assert post(client, new_alex(), sig="deadbeef").status_code == 401
    assert api.pipeline().store.all_hires() == []


def test_event_status_and_idempotent_replay(client):
    first = post(client, new_alex()).json()
    assert first["outcome"] == "processed" and len(first["ops"]) == 5
    assert post(client, new_alex()).json()["outcome"] == "duplicate"
    st = client.get("/hires/H-1001/status").json()
    assert st["hire"]["legal_first_name"] == "Alex" and len(st["ops"]) == 5
    assert client.get("/hires/H-9/status").status_code == 404


def test_exception_list_and_resolve(client):
    post(client, new_alex())
    api.pipeline().equipment.mark_shipped("H-1001")
    out = post(client, new_alex("e2", 2, addr={**ADDR, "line1": "9 New St"})).json()
    case = out["exceptions"][0]["case_id"]
    assert [c["case_id"] for c in client.get("/exceptions?status=awaiting_human").json()] == [case]
    assert client.post(f"/exceptions/{case}/resolve", json={"decision": "approve"}).json()["status"] == "approved"
    assert client.post(f"/exceptions/{case}/resolve", json={"decision": "maybe"}).status_code == 422


def test_invalid_event_422(client):
    body = b'{"nope": 1}'
    r = client.post("/events", content=body, headers={"X-Signature": api.sign(body)})
    assert r.status_code == 422


def test_check_deadlines_flags_overdue(client):
    post(client, new_alex())  # equipment + okta done -> those deadlines clear
    p = api.pipeline()
    p.today = date(2026, 11, 20)  # past bg_check_due (start-7d = 11-09), still open
    assert [(i["kind"], i["state"]) for i in p.check_deadlines()] == [("bg_check_due", "overdue")]
