"""Adapter contract, error taxonomy, failure injection and JSON-file mock state."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol


class TransientError(Exception):
    """Timeout / 429 / 5xx: safe to retry."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class PermanentError(Exception):
    """4xx / validation / conflict: retrying cannot help, a human must look."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TargetAdapter(Protocol):
    """What the executor needs from a target system.

    Real API clients implement this same method so the pipeline is unchanged.
    Implementations must be idempotent per (hire_id, action): a crash between
    the remote write and our ledger update must be safe to replay.
    """

    name: str

    def apply(self, action: str, hire_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...


class FailureConfig:
    """Failure injection for demos/tests.

    Shape (JSON, which is valid YAML so config/failures.yaml needs no extra dependency):
      {"equipment": {"fail_first_calls": 2, "status": 503},
       "okta": {"conflict_logins": ["sam.lee@betterup.co"]}}
    Env SYNC_FAILURES (JSON) overrides the file.
    """

    def __init__(self, spec: dict[str, Any] | None = None):
        self.spec: dict[str, Any] = spec or {}
        self.calls: dict[str, int] = {}

    @classmethod
    def load(cls, path: str | Path | None = "config/failures.yaml") -> "FailureConfig":
        env = os.environ.get("SYNC_FAILURES")
        if env:
            return cls(json.loads(env))
        if path and Path(path).exists():
            text = Path(path).read_text().strip()
            return cls(json.loads(text) if text else {})
        return cls()

    def configure(self, spec: dict[str, Any] | None) -> None:
        self.spec = spec or {}
        self.calls = {}

    def check(self, system: str, key: str | None = None) -> None:
        cfg = self.spec.get(system, {})
        self.calls[system] = self.calls.get(system, 0) + 1
        if self.calls[system] <= int(cfg.get("fail_first_calls", 0)):
            status = int(cfg.get("status", 503))
            raise TransientError(f"{system} returned {status} (injected)", status)
        if key is not None and key in cfg.get("conflict_logins", []):
            raise PermanentError(f"{system} returned 409 conflict for {key} (injected)", 409)


class JsonState:
    """Tiny JSON-file persistence so the demo shows real before/after state."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"records": {}, "writes": 0}

    def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


class MockAdapter:
    """Shared plumbing: a per-hire record dict plus a write counter."""

    name = "mock"

    def __init__(self, data_dir: Path, failures: FailureConfig):
        self.state = JsonState(data_dir / "mock_state" / f"{self.name}.json")
        self.failures = failures

    def _commit(self, data: dict[str, Any]) -> None:
        data["writes"] = data.get("writes", 0) + 1
        self.state.save(data)

    def snapshot(self) -> dict[str, Any]:
        return self.state.load()

    def writes(self) -> int:
        return int(self.state.load().get("writes", 0))
