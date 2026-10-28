"""Runs scenarios S1-S8 against a fresh data dir and prints what happened."""
from __future__ import annotations

import json
import shutil
import sys
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.table import Table

from sync_engine.adapters.base import FailureConfig
from sync_engine.models import SourceEvent
from sync_engine.pipeline import Pipeline

DATA = Path("data")
SCEN = Path("mocks/scenarios")
console = Console()


def snapshot(p: Pipeline) -> dict:
    return {a.name: a.snapshot()["records"] for a in p.adapters.values()}


def total_writes(p: Pipeline) -> int:
    return sum(a.writes() for a in p.adapters.values())


def show_report(rep) -> None:
    console.print(f"  [bold]event[/] {rep.event_id} -> outcome=[cyan]{rep.outcome}[/] hire={rep.hire_id}")
    if rep.changes:
        t = Table("field", "old", "new", title="field diffs", show_lines=False)
        for c in rep.changes:
            t.add_row(c.field, str(c.old)[:45], str(c.new)[:45])
        console.print(t)
    for i in rep.issues:
        colour = "red" if i.severity == "block" else "yellow"
        console.print(f"  [{colour}]{i.rule_id} {i.severity}[/] {i.field}: {i.message}")
    if rep.results:
        t = Table("system", "action", "status", "attempts", "error", title="ops")
        for r in rep.results:
            t.add_row(r.system, r.action, r.status, str(r.attempts), (r.error or "")[:60])
        console.print(t)
    for c in rep.cases:
        d = c.decision
        extra = f" | Claude[{d['mode']}]: {d['decision']} conf={d['confidence']:.2f}" if d else ""
        console.print(f"  [magenta]exception[/] {c.case_id} {c.kind} -> {c.status}{extra}")


def main() -> None:
    shutil.rmtree(DATA, ignore_errors=True)
    # Pinned "today" so V1/V2 results are reproducible regardless of when the demo is run.
    p = Pipeline(DATA, today=date(2026, 11, 2), failures=FailureConfig(), base_delay=0.01)
    console.rule("BetterUp onboarding sync demo")
    console.print(f"Claude mode: [bold]{'API (' + p.claude.model + ')' if p.claude.mode == 'claude' else 'offline fallback (no ANTHROPIC_API_KEY) -> needs_human'}[/]")
    before = snapshot(p)

    for path in sorted(SCEN.glob("S*.json")):
        sc = json.loads(path.read_text())
        console.rule(f"{sc['id']}: {sc['title']}")
        setup = sc.get("setup", {})
        p.failures.configure(setup.get("failures"))
        for hid in setup.get("mark_shipped", []):
            p.equipment.mark_shipped(hid)
            console.print(f"  setup: equipment for {hid} marked shipped")
        writes_before, done_before = total_writes(p), len(p.store.ledger_list("done"))
        for raw in sc["events"]:
            show_report(p.process(SourceEvent.model_validate(raw)))
        if sc["id"] == "S5":
            new_writes = total_writes(p) - writes_before
            new_done = len(p.store.ledger_list("done")) - done_before
            console.print(f"  [bold green]replayed event: {new_writes} new writes[/] (ledger 'done' rows added: {new_done})")
        p.failures.configure(None)

    console.rule("Ops ledger (all hires)")
    t = Table("hire", "system", "action", "status", "attempts")
    for r in p.store.ledger_list():
        t.add_row(r["hire_id"], r["system"], r["action"], r["status"], str(r["attempts"]))
    console.print(t)

    console.rule("Open exceptions (see data/slack_outbox.jsonl)")
    for c in p.store.list_exceptions():
        console.print(f"  {c.case_id} {c.status:<15} {c.kind:<26} {c.hire_id}")

    console.rule("Mock system state: before vs after")
    after = snapshot(p)
    for system in after:
        console.print(f"  {system}: {len(before[system])} records -> {len(after[system])} records, writes={p.adapters[system].writes()}")

    console.rule("Audit trail for H-1001")
    t = Table("#", "stage", "event", "detail")
    for r in p.store.audit_for("H-1001"):
        t.add_row(str(r["id"]), r["stage"], r["event_id"] or "", r["detail_json"][:90])
    console.print(t)


if __name__ == "__main__":
    sys.exit(main())
