from types import SimpleNamespace

from sync_engine.claude_handler import ClaudeHandler, enforce_policy
from sync_engine.models import ExceptionDecision


class FakeClient:
    def __init__(self, tool_input=None, raises=None):
        self.tool_input, self.raises = tool_input, raises
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        if self.raises:
            raise self.raises
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=self.tool_input)])


GOOD = {"decision": "apply", "confidence": 0.95, "reasoning": "r", "slack_message": "m"}


def decide(client):
    return ClaudeHandler(client=client).decide("cross_source_mismatch", "H-1", {"x": 1}, "summary")


def test_no_api_key_falls_back_to_needs_human():
    r = ClaudeHandler(api_key="").decide("cross_source_mismatch", "H-1", {}, "s")
    assert r.mode == "fallback" and r.decision.decision == "needs_human"


def test_invalid_output_falls_back():
    r = decide(FakeClient({"decision": "apply", "confidence": 7, "reasoning": "r"}))
    assert r.mode == "fallback" and r.decision.decision == "needs_human"


def test_api_error_falls_back():
    assert decide(FakeClient(raises=TimeoutError())).mode == "fallback"


def test_valid_output_used_and_hash_recorded():
    r = decide(FakeClient(GOOD))
    assert r.mode == "claude" and r.decision.decision == "apply" and len(r.prompt_hash) == 12


def test_identity_field_never_auto_applied_even_at_full_confidence():
    d = ExceptionDecision(**{**GOOD, "confidence": 1.0})
    assert enforce_policy(d, "cross_source_mismatch", {"legal_name"})[0] is False
    assert enforce_policy(d, "cross_source_mismatch", {"legal_first_name"})[0] is False


def test_non_identity_needs_apply_and_threshold():
    ok = ExceptionDecision(**{**GOOD, "confidence": 0.85})
    low = ExceptionDecision(**{**GOOD, "confidence": 0.84})
    hold = ExceptionDecision(**{**GOOD, "decision": "hold", "confidence": 0.99})
    assert enforce_policy(ok, "cross_source_mismatch", {"start_date"})[0] is True
    assert enforce_policy(low, "cross_source_mismatch", {"start_date"})[0] is False
    assert enforce_policy(hold, "cross_source_mismatch", {"start_date"})[0] is False


def test_shipped_cases_always_need_human():
    assert enforce_policy(ExceptionDecision(**GOOD), "post_shipment_address", {"ship_address"})[0] is False
