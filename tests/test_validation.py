from datetime import date

from sync_engine import validation as v
from sync_engine.models import Address, Location
from tests.test_planner import hire

TODAY = date(2026, 11, 2)


def sev(issues):
    return [(i.rule_id, i.severity) for i in issues]


def test_v1():
    assert v.v1(hire(), None, None, TODAY) == []
    assert sev(v.v1(hire(start_date=date(2026, 10, 1)), None, None, TODAY)) == [("V1", "block")]
    assert sev(v.v1(hire(start_date=date(2026, 11, 14)), None, None, TODAY)) == [("V1", "block")]  # Saturday
    old = hire(start_date=date(2026, 10, 1))  # unchanged past date on an existing hire is not re-blocked
    assert v.v1(old, old, None, TODAY) == []


def test_v2():
    old = hire(start_date=date(2026, 11, 16))
    assert sev(v.v2(hire(start_date=date(2026, 11, 9)), old, None, TODAY)) == [("V2", "warn")]
    assert v.v2(hire(start_date=date(2026, 12, 7)), old, None, date(2026, 10, 1)) == []
    assert v.v2(hire(), None, None, TODAY) == []


def test_v3():
    assert v.v3(hire(), None, None, TODAY) == []
    assert ("V3", "block") in sev(v.v3(hire(legal_first_name="Al3x"), None, None, TODAY))
    assert ("V3", "block") in sev(v.v3(hire(legal_last_name="Rivera "), None, None, TODAY))
    assert sev(v.v3(hire(legal_last_name="RIVERA"), None, None, TODAY)) == [("V3", "warn")]


def test_v4():
    assert v.v4(hire(), None, None, TODAY) == []
    bad = Address(line1="", city="Austin", postal_code="ABC", country="US")
    assert len(v.v4(hire(ship_address=bad), None, None, TODAY)) == 2  # missing line1 + bad postal
    ca = Address(line1="1 A", city="Toronto", postal_code="M5V 2T6", country="CA")
    assert v.v4(hire(ship_address=ca), None, None, TODAY) == []


def test_v5():
    assert sev(v.v5(hire(work_location=Location(city="Paris", country="FR")), None, None, TODAY)) == [("V5", "warn")]
    assert v.v5(hire(work_location=Location(city="Paris", country="FR", remote=True)), None, None, TODAY) == []
    assert v.v5(hire(), None, None, TODAY) == []


def test_v6():
    assert v.v6(hire(), None, None, TODAY) == []
    assert sev(v.v6(hire(personal_email="nope"), None, None, TODAY)) == [("V6", "block")]


def test_v7():
    a = hire(source="ashby")
    assert v.v7(hire(source="workday"), None, a, TODAY) == []
    got = v.v7(hire(source="workday", legal_first_name="Alexander", start_date=date(2026, 11, 23)), None, a, TODAY)
    assert {i.field for i in got} == {"legal_name", "start_date"}
    assert v.v7(hire(), None, None, TODAY) == []


def test_v8():
    assert v.v8(hire(), None, None, TODAY) == []
    assert sev(v.v8(hire(manager_email="m@gmail.com"), None, None, TODAY)) == [("V8", "block")]
