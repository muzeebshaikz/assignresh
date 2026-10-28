"""Mock Workday as a WRITE target (system of record for HR data).

Workday is also an event source; the planner never writes a change back to the system it came from.
# ASSUMPTION: pre-hire/worker field names are invented, not Workday's real schema.
"""
from __future__ import annotations

from typing import Any

from .base import MockAdapter, PermanentError


class WorkdayAdapter(MockAdapter):
    name = "workday"

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.state.load()
        recs = data["records"]
        clean = {k: v for k, v in payload.items() if k != "as_of"}
        if action == "create_prehire":
            self.failures.check(self.name)
            recs.setdefault(hire_id, {}).update(clean)  # upsert by hire_id: replay-safe
        elif action == "update_worker":
            if hire_id not in recs:
                raise PermanentError("404 no workday record for hire", 404)
            self.failures.check(self.name)
            recs[hire_id].update(clean)
        else:
            raise PermanentError(f"unknown workday action {action}", 400)
        self._commit(data)
        return recs[hire_id]
