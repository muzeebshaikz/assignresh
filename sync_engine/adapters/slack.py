"""Mock Slack: appends Block-Kit-like messages (with approve/reject buttons) to a JSONL outbox.

# ASSUMPTION: payloads are Block-Kit-shaped but not validated against Slack's API.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CHANNEL = "#onboarding-exceptions"


class SlackAdapter:
    name = "slack"

    def __init__(self, data_dir: Path):
        self.outbox = data_dir / "slack_outbox.jsonl"
        self.outbox.parent.mkdir(parents=True, exist_ok=True)

    def post_exception(self, case_id: str, kind: str, hire_id: str, text: str, needs_approval: bool) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{kind}* · hire `{hire_id}` · case `{case_id}`\n{text}"}}
        ]
        if needs_approval:
            blocks.append({"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Approve"}, "style": "primary",
                 "action_id": "exception_approve", "value": case_id},
                {"type": "button", "text": {"type": "plain_text", "text": "Reject"}, "style": "danger",
                 "action_id": "exception_reject", "value": case_id},
            ]})
        msg = {"channel": CHANNEL, "text": f"[{kind}] {hire_id}", "blocks": blocks}
        with self.outbox.open("a") as f:
            f.write(json.dumps(msg) + "\n")
        return msg

    def messages(self) -> list[dict[str, Any]]:
        if not self.outbox.exists():
            return []
        return [json.loads(l) for l in self.outbox.read_text().splitlines() if l.strip()]
