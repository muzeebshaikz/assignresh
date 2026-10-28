"""Claude as a component: exception triage only, never the pipeline's decision-maker.

Layers (outermost wins):
  1. Rules run first and decide everything they can.
  2. Claude gets a minimal context and must answer via a forced tool call (JSON schema).
  3. Output is validated with Pydantic; any failure -> deterministic `needs_human`.
  4. `enforce_policy` (plain code, not prompt) decides if the answer may auto-apply.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .models import ExceptionDecision

PROMPT_PATH = Path(__file__).parent / "prompts" / "exception_handler.md"
AUTO_APPLY_MIN_CONFIDENCE = 0.85
IDENTITY_FIELDS = {"legal_first_name", "legal_last_name", "legal_name", "login"}
# Physical/irreversible situations: Claude may triage and draft, but a human always signs off.
HUMAN_ONLY_KINDS = {"post_shipment_address", "late_start_change", "legal_name_active_account", "identity_ambiguous"}
TOOL = {
    "name": "record_decision",
    "description": "Record the exception-handling decision.",
    "input_schema": ExceptionDecision.model_json_schema(),
}


@dataclass
class DecisionResult:
    decision: ExceptionDecision
    mode: str  # "claude" | "fallback"
    model: str | None
    prompt_hash: str
    error: str | None = None


def fallback(kind: str, hire_id: str, summary: str, why: str) -> ExceptionDecision:
    return ExceptionDecision(
        decision="needs_human", recommended_value=None, confidence=0.0,
        reasoning=f"Deterministic fallback ({why}).",
        slack_message=f"[{kind}] hire {hire_id}: {summary} Needs human review.",
    )


def enforce_policy(decision: ExceptionDecision, kind: str, fields: set[str]) -> tuple[bool, str]:
    """Return (may_auto_apply, reason). Lives in code so a prompt injection cannot weaken it."""
    if fields & IDENTITY_FIELDS:
        return False, "identity field: never auto-applied"
    if kind in HUMAN_ONLY_KINDS:
        return False, f"{kind}: human approval always required"
    if decision.decision != "apply":
        return False, f"decision={decision.decision}"
    if decision.confidence < AUTO_APPLY_MIN_CONFIDENCE:
        return False, f"confidence {decision.confidence:.2f} < {AUTO_APPLY_MIN_CONFIDENCE}"
    return True, "decision=apply with sufficient confidence"


class ClaudeHandler:
    def __init__(self, api_key: str | None = None, model: str | None = None, client: Any = None, timeout: float = 20.0):
        self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY")
        self.model = model or os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
        self.client, self.timeout = client, timeout

    @property
    def mode(self) -> str:
        return "claude" if (self.client or self.api_key) else "fallback"

    def decide(self, kind: str, hire_id: str, context: dict[str, Any], summary: str) -> DecisionResult:
        system = PROMPT_PATH.read_text()
        user = json.dumps({"kind": kind, "hire_id": hire_id, "context": context}, sort_keys=True, default=str)
        phash = hashlib.sha256((system + user).encode()).hexdigest()[:12]

        def fb(why: str) -> DecisionResult:
            return DecisionResult(fallback(kind, hire_id, summary, why), "fallback", None, phash, why)

        if self.mode == "fallback":
            return fb("no ANTHROPIC_API_KEY")
        try:
            client = self.client or self._make_client()
            resp = client.messages.create(
                model=self.model, max_tokens=800, temperature=0, system=system, timeout=self.timeout,
                tools=[TOOL], tool_choice={"type": "tool", "name": "record_decision"},
                messages=[{"role": "user", "content": user}],
            )
            block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
            if block is None:
                return fb("no tool_use block in response")
            return DecisionResult(ExceptionDecision.model_validate(block.input), "claude", self.model, phash)
        except ValidationError as e:
            return fb(f"invalid output: {e.error_count()} schema errors")
        except Exception as e:  # noqa: BLE001 - any API/network/SDK failure degrades to human review, audited by caller
            return fb(f"api error: {type(e).__name__}")

    def _make_client(self) -> Any:
        import anthropic  # lazy: the offline path must not require the SDK

        return anthropic.Anthropic(api_key=self.api_key)
