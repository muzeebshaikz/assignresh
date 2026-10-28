"""Idempotent op execution with retry, per-system isolation and dead-lettering."""
from __future__ import annotations

import random
import time
from typing import Callable

from .adapters.base import PermanentError, TargetAdapter, TransientError
from .models import OpResult, PlannedOp
from .store import Store


class Executor:
    def __init__(self, store: Store, adapters: dict[str, TargetAdapter], sleep: Callable[[float], None] = time.sleep,
                 base_delay: float = 0.5, max_attempts: int = 3, rng: random.Random | None = None):
        self.store, self.adapters = store, adapters
        self.sleep, self.base_delay, self.max_attempts = sleep, base_delay, max_attempts
        self.rng = rng or random.Random()

    def run(self, ops: list[PlannedOp], event_id: str | None, force: bool = False) -> list[OpResult]:
        # Each op is independent: one system failing never stops the loop (partial success is recorded).
        return [self.run_one(op, event_id, force) for op in ops]

    def _result(self, op: PlannedOp, status: str, attempts: int, error: str | None = None) -> OpResult:
        return OpResult(status=status, attempts=attempts, error=error, system=op.system,  # type: ignore[arg-type]
                        action=op.action, idempotency_key=op.idempotency_key)

    def run_one(self, op: PlannedOp, event_id: str | None, force: bool = False) -> OpResult:
        row = self.store.ledger_get(op.idempotency_key)
        if row and row["status"] == "done":
            self.store.audit(op.hire_id, event_id, "op_skipped_duplicate", {"system": op.system, "action": op.action})
            return self._result(op, "skipped_duplicate", 0)
        if row and row["status"] == "failed_permanent" and not force:
            return self._result(op, "failed_permanent", 0, row["last_error"])

        prior = row["attempts"] if row else 0
        self.store.ledger_set(op, "pending", prior, None)
        attempts = 0
        while True:
            attempts += 1
            try:
                self.adapters[op.system].apply(op.action, op.hire_id, op.payload)
            except PermanentError as e:
                return self._fail(op, event_id, "failed_permanent", prior, attempts, str(e))
            except TransientError as e:
                if attempts >= self.max_attempts:
                    return self._fail(op, event_id, "failed_transient", prior, attempts, str(e))
                self.store.audit(op.hire_id, event_id, "op_retry", {"system": op.system, "attempt": attempts, "error": str(e)})
                # exponential backoff with jitter so concurrent retries don't stampede a recovering system
                self.sleep(self.base_delay * 2 ** (attempts - 1) * (0.5 + self.rng.random()))
            except Exception as e:  # noqa: BLE001 - classified and audited, never swallowed
                return self._fail(op, event_id, "failed_permanent", prior, attempts, f"unexpected {type(e).__name__}: {e}")
            else:
                self.store.ledger_set(op, "done", prior + attempts, None)
                self.store.audit(op.hire_id, event_id, "op_done", {"system": op.system, "action": op.action, "attempts": attempts})
                return self._result(op, "done", attempts)

    def _fail(self, op: PlannedOp, event_id: str | None, status: str, prior: int, attempts: int, err: str) -> OpResult:
        self.store.ledger_set(op, status, prior + attempts, err)
        self.store.audit(op.hire_id, event_id, f"op_{status}", {"system": op.system, "action": op.action, "attempts": attempts, "error": err})
        return self._result(op, status, attempts, err)
