"""Mock Cohort Tracker (Google Sheet). # ASSUMPTION: column names are invented."""
from __future__ import annotations

from typing import Any

from .base import MockAdapter, PermanentError


class TrackerAdapter(MockAdapter):
    name = "tracker"

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.state.load()
        rows = data["records"]
        clean = {k: v for k, v in payload.items() if k != "as_of"}
        if action == "add_row":
            self.failures.check(self.name)
            rows.setdefault(hire_id, {}).update(clean)  # keyed by hire_id: never a duplicate row
        elif action == "update_row":
            if hire_id not in rows:
                raise PermanentError("404 row not found", 404)
            self.failures.check(self.name)
            rows[hire_id].update(clean)
        else:
            raise PermanentError(f"unknown tracker action {action}", 400)
        self._commit(data)
        return rows[hire_id]
