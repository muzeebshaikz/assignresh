"""Regenerates mocks/scenarios/*.json (kept as a script so payload shapes stay consistent)."""
import json
from pathlib import Path

OUT = Path(__file__).parent / "scenarios"
ADDR = {"line1": "12 Congress Ave", "line2": None, "city": "Austin", "state": "TX", "postalCode": "78701", "country": "US"}


def ashby(eid, ver, cand, first, last, email, start, addr=ADDR, kind="hire.updated", pref=None, mgr="dana.cho@betterup.co",
          loc=("Austin", "US", False), dept="Coaching"):
    return {"event_id": eid, "source": "ashby", "event_type": kind, "source_version": ver,
            "occurred_at": f"2026-11-0{min(ver, 9)}T09:00:00Z",
            "payload": {"candidate": {"id": cand, "firstName": first, "lastName": last, "preferredName": pref, "email": email},
                        "offer": {"startDate": start, "department": dept, "employmentType": "FTE", "hiringManagerEmail": mgr},
                        "location": {"city": loc[0], "country": loc[1], "remote": loc[2]}, "shippingAddress": addr}}


def workday(eid, ver, wid, first, last, email, start, addr=ADDR, kind="hire.updated", mgr="dana.cho@betterup.co",
            loc=("Austin", "US", False), dept="Coaching"):
    a = dict(addr)
    return {"event_id": eid, "source": "workday", "event_type": kind, "source_version": ver,
            "occurred_at": f"2026-11-0{min(ver, 9)}T10:00:00Z",
            "payload": {"worker": {"workerId": wid, "legalName": {"first": first, "last": last}, "preferredName": None,
                                   "contactEmail": email, "hireDate": start, "managerEmail": mgr, "department": dept,
                                   "workerType": "Employee", "workLocation": {"city": loc[0], "country": loc[1], "remote": loc[2]},
                                   "homeAddress": a}}}


NEW_ADDR = {**ADDR, "line1": "900 Lamar Blvd", "postalCode": "78703"}
s1 = ashby("evt-s1-001", 1, "cand-alex", "Alex", "Rivera", "alex.rivera@example.com", "2026-11-16", kind="hire.created")
s2 = ashby("evt-s2-001", 2, "cand-alex", "Alex", "Rivera", "alex.rivera@example.com", "2026-11-09")
S = [
    {"id": "S1", "title": "New hire from Ashby (happy path)", "setup": {}, "events": [s1]},
    {"id": "S2", "title": "Start date moved after offer (V2 warning)", "setup": {}, "events": [s2]},
    {"id": "S3", "title": "Ashby 'Jon Smith' vs Workday 'Jonathan Smyth' (V7 -> Claude -> Slack)", "setup": {}, "events": [
        ashby("evt-s3-001", 1, "cand-jon", "Jon", "Smith", "jon.smith@example.com", "2026-11-23", kind="hire.created"),
        workday("evt-s3-002", 1, "WD-5521", "Jonathan", "Smyth", "jon.smith@example.com", "2026-11-23", kind="hire.created")]},
    {"id": "S4", "title": "Off-cycle hire created only in Workday", "setup": {}, "events": [
        workday("evt-s4-001", 1, "WD-6102", "Priya", "Nair", "priya.nair@example.com", "2026-11-30", kind="hire.created",
                loc=("Bengaluru", "IN", False), addr={**ADDR, "line1": "4 MG Road", "city": "Bengaluru", "state": "KA", "postalCode": "560001", "country": "IN"})]},
    {"id": "S5", "title": "Same event replayed twice -> zero new writes", "setup": {}, "events": [s2]},
    {"id": "S6", "title": "Equipment 503 x2 then OK; Okta 409 permanent; Lumos/Tracker unaffected",
     "setup": {"failures": {"equipment": {"fail_first_calls": 2, "status": 503}, "okta": {"conflict_logins": ["sam.lee@betterup.co"]}}},
     "events": [ashby("evt-s6-001", 1, "cand-sam", "Sam", "Lee", "sam.lee@example.com", "2026-12-07", kind="hire.created")]},
    {"id": "S7", "title": "Address change after laptop shipped -> exception, no equipment write",
     "setup": {"mark_shipped": ["H-1001"]},
     "events": [ashby("evt-s7-001", 3, "cand-alex", "Alex", "Rivera", "alex.rivera@example.com", "2026-11-09", addr=NEW_ADDR)]},
    {"id": "S8", "title": "Out-of-order (stale) event ignored", "setup": {},
     "events": [ashby("evt-s8-001", 2, "cand-alex", "Alex", "Rivera", "alex.rivera@example.com", "2026-11-23")]},
]
for s in S:
    (OUT / f"{s['id']}.json").write_text(json.dumps(s, indent=2))
print("wrote", len(S))
