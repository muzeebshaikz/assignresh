from datetime import date

import pytest

from sync_engine import differ, planner
from sync_engine.models import Address, Hire, Location


def hire(**kw):
    base = dict(hire_id="H-1", legal_first_name="Alex", legal_last_name="Rivera", personal_email="a@x.com",
                start_date=date(2026, 11, 16), work_location=Location(city="Austin", country="US"),
                ship_address=Address(line1="1 A St", city="Austin", postal_code="78701", country="US"),
                manager_email="m@betterup.co", department="Coaching", employment_type="FTE", source="ashby",
                source_version=2, updated_at="2026-11-01T00:00:00Z")
    base.update(kw)
    return Hire(**base)


def systems(old, new, facts=None):
    res = planner.plan(old, new, differ.diff(old, new), facts or planner.Facts())
    return {o.system for o in res.ops}


OLD = hire()
MATRIX = [  # field changed -> systems that must be written
    ("start_date", date(2026, 11, 23), {"okta", "lumos", "equipment", "tracker", "workday"}),
    ("preferred_name", "Lex", {"okta", "tracker", "workday"}),
    ("ship_address", Address(line1="2 B St", city="Austin", postal_code="78702", country="US"), {"equipment", "workday"}),
    ("work_location", Location(city="Denver", country="US"), {"okta", "lumos", "tracker", "workday"}),
    ("manager_email", "n@betterup.co", {"okta", "lumos", "tracker", "workday"}),
    ("legal_last_name", "Rivers", {"okta", "equipment", "tracker", "workday"}),
    ("department", "Sales", {"workday"}),
    ("employment_type", "contractor", set()),
]


@pytest.mark.parametrize("field,value,expected", MATRIX)
def test_field_maps_to_correct_systems(field, value, expected):
    assert systems(OLD, hire(**{field: value, "source_version": 3})) == expected


def test_diff_only_changed_fields_and_none_for_identical():
    new = hire(manager_email="n@betterup.co", source_version=3)
    assert [c.field for c in differ.diff(OLD, new)] == ["manager_email"]
    assert differ.diff(OLD, hire(source_version=9)) == []


def test_new_hire_creates_in_all_five():
    res = planner.plan(None, OLD, differ.diff(None, OLD), planner.Facts())
    assert {o.system for o in res.ops} == {"okta", "lumos", "equipment", "tracker", "workday"}


def test_workday_sourced_change_is_never_written_back_to_workday():
    wd = hire(source="workday")
    assert systems(None, wd) == {"okta", "lumos", "equipment", "tracker"}
    assert "workday" not in systems(OLD, hire(source="workday", manager_email="n@betterup.co", source_version=3))


def test_shipped_address_still_updates_workday_record():
    new = hire(ship_address=Address(line1="2 B", city="Austin", postal_code="78702", country="US"), source_version=3)
    res = planner.plan(OLD, new, differ.diff(OLD, new), planner.Facts(shipped=True))
    assert {o.system for o in res.ops} == {"workday"}


def test_ship_by_is_five_business_days_before_start():
    assert planner.ship_by(date(2026, 11, 16)) == date(2026, 11, 9)
    assert planner.ship_by(date(2026, 11, 11)) == date(2026, 11, 4)


def test_login_is_ascii_folded_lowercase():
    assert planner.login_base("José", "O'Brien") == "jose.obrien"


def test_shipped_equipment_gets_no_ops_for_name_or_address():
    new = hire(legal_last_name="Rivers", ship_address=Address(line1="2 B", city="Austin", postal_code="78702", country="US"),
               source_version=3)
    res = planner.plan(OLD, new, differ.diff(OLD, new), planner.Facts(shipped=True))
    assert "equipment" not in {o.system for o in res.ops}
    assert [e.kind for e in res.exceptions] == ["post_shipment_address"]


def test_earlier_start_after_shipment_raises_late_start_exception():
    new = hire(start_date=date(2026, 11, 9), source_version=3)
    res = planner.plan(OLD, new, differ.diff(OLD, new), planner.Facts(shipped=True))
    assert [e.kind for e in res.exceptions] == ["late_start_change"]
    assert "equipment" not in {o.system for o in res.ops}


def test_idempotency_key_stable_and_payload_sensitive():
    a = planner.plan(None, OLD, differ.diff(None, OLD), planner.Facts()).ops
    b = planner.plan(None, OLD, differ.diff(None, OLD), planner.Facts()).ops
    assert [o.idempotency_key for o in a] == [o.idempotency_key for o in b]
    other = hire(manager_email="z@betterup.co")
    c = planner.plan(None, other, differ.diff(None, other), planner.Facts()).ops
    assert {o.idempotency_key for o in a} != {o.idempotency_key for o in c}
