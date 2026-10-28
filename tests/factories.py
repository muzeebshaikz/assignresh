"""Event builders shared by tests (same shapes as mocks/scenarios)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mocks"))
from build_scenarios import ADDR, ashby, workday  # noqa: E402,F401
from sync_engine.models import SourceEvent  # noqa: E402


def ev(raw: dict) -> SourceEvent:
    return SourceEvent.model_validate(raw)


def new_alex(eid="e1", ver=1, start="2026-11-16", **kw):
    return ev(ashby(eid, ver, "cand-alex", "Alex", "Rivera", "alex.rivera@example.com", start,
                    kind="hire.created" if ver == 1 else "hire.updated", **kw))
