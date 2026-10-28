"""Mock Lumos. # ASSUMPTION: bundle/approver fields are invented for the prototype."""
from __future__ import annotations

from typing import Any

from .base import MockAdapter, PermanentError


class LumosAdapter(MockAdapter):
    name = "lumos"

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.state.load()
        recs = data["records"]
        clean = {k: v for k, v in payload.items() if k != "as_of"}
        if action == "request_bundle":
            self.failures.check(self.name)
            recs.setdefault(hire_id, {}).update(clean)  # upsert: replay-safe
        elif action == "update_access":
            if hire_id not in recs:
                raise PermanentError("404 no access request for hire", 404)
            self.failures.check(self.name)
            recs[hire_id].update(clean)
        else:
            raise PermanentError(f"unknown lumos action {action}", 400)
        self._commit(data)
        return recs[hire_id]
