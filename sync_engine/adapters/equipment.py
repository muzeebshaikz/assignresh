"""Mock equipment (IT hardware form / shipping). # ASSUMPTION: invented shape."""
from __future__ import annotations

from typing import Any

from .base import MockAdapter, PermanentError


class EquipmentAdapter(MockAdapter):
    name = "equipment"

    def is_shipped(self, hire_id: str) -> bool:
        return bool(self.state.load()["records"].get(hire_id, {}).get("shipped"))

    def mark_shipped(self, hire_id: str) -> None:
        data = self.state.load()
        data["records"][hire_id]["shipped"] = True
        self.state.save(data)

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.state.load()
        recs = data["records"]
        clean = {k: v for k, v in payload.items() if k != "as_of"}
        if action == "create_request":
            self.failures.check(self.name)
            if hire_id not in recs:
                recs[hire_id] = {**clean, "shipped": False}
        elif action == "update_request":
            if hire_id not in recs:
                raise PermanentError("404 no hardware request for hire", 404)
            # Defence in depth: the planner should already have diverted this to an exception.
            if recs[hire_id].get("shipped") and "ship_address" in clean and not clean.get("reship_approved"):
                raise PermanentError("409 package already shipped", 409)
            self.failures.check(self.name)
            recs[hire_id].update(clean)
        else:
            raise PermanentError(f"unknown equipment action {action}", 400)
        self._commit(data)
        return recs[hire_id]
