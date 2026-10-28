"""Mock Okta. # ASSUMPTION: attribute names are invented, not Okta's real schema."""
from __future__ import annotations

from typing import Any

from .base import MockAdapter, PermanentError

DOMAIN = "betterup.co"


class OktaAdapter(MockAdapter):
    name = "okta"

    def is_active(self, hire_id: str) -> bool:
        return self.state.load()["records"].get(hire_id, {}).get("status") == "ACTIVE"

    def mark_active(self, hire_id: str) -> None:
        data = self.state.load()
        data["records"][hire_id]["status"] = "ACTIVE"
        self.state.save(data)

    def _free_login(self, base: str, hire_id: str, records: dict[str, Any]) -> str:
        """first.last@domain; on collision with another hire append a digit."""
        taken = {r["login"] for h, r in records.items() if h != hire_id}
        login, n = f"{base}@{DOMAIN}", 1
        while login in taken:
            n += 1
            login = f"{base}{n}@{DOMAIN}"
        return login

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.state.load()
        recs = data["records"]
        if action == "create_user":
            if hire_id in recs:  # replay after a crash: already created
                return recs[hire_id]
            login = self._free_login(payload["login_base"], hire_id, recs)
            self.failures.check(self.name, key=login)
            recs[hire_id] = {
                "login": login, "first_name": payload["first_name"], "last_name": payload["last_name"],
                "display_name": payload["display_name"], "status": "STAGED",
                "activation_date": payload["activation_date"], "region_group": payload["region_group"],
                "manager": payload["manager"],
            }
        elif action == "update_user":
            if hire_id not in recs:
                raise PermanentError("404 okta user does not exist", 404)
            self.failures.check(self.name)
            rec = recs[hire_id]
            for k, v in payload.items():
                if k == "as_of":
                    continue
                if k == "login_base":
                    rec["login"] = self._free_login(v, hire_id, recs)
                else:
                    rec[k] = v
        else:
            raise PermanentError(f"unknown okta action {action}", 400)
        self._commit(data)
        return recs[hire_id]
