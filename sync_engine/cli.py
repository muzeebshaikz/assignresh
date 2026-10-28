"""Operator CLI. Data dir from SYNC_DATA_DIR (default ./data)."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .models import SourceEvent
from .pipeline import Pipeline


def _pipeline() -> Pipeline:
    return Pipeline(os.environ.get("SYNC_DATA_DIR", "data"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="sync_engine.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dead-letters")
    sub.add_parser("retry")
    sub.add_parser("exceptions")
    sub.add_parser("check-deadlines")
    a = sub.add_parser("audit"); a.add_argument("hire_id")
    r = sub.add_parser("resolve"); r.add_argument("case_id"); r.add_argument("decision", choices=["approve", "reject"])
    i = sub.add_parser("ingest"); i.add_argument("file")
    args = ap.parse_args(argv)
    p = _pipeline()

    if args.cmd == "audit":
        for row in p.store.audit_for(args.hire_id):
            print(f"{row['id']:>4} {row['ts']} {row['stage']:<22} {row['detail_json']}")
    elif args.cmd == "dead-letters":
        for row in p.store.ledger_list("failed_transient"):
            print(f"{row['hire_id']} {row['system']}.{row['action']} attempts={row['attempts']} err={row['last_error']}")
    elif args.cmd == "retry":
        for res in p.retry_dead_letters():
            print(f"{res.system}.{res.action}: {res.status} (attempts={res.attempts})")
    elif args.cmd == "exceptions":
        for c in p.store.list_exceptions():
            print(f"{c.case_id} {c.status:<15} {c.kind:<26} {c.hire_id} held_ops={len(c.held_ops)}")
    elif args.cmd == "check-deadlines":
        for it in p.check_deadlines():
            print(it)
    elif args.cmd == "resolve":
        c = p.resolve(args.case_id, args.decision)
        print(f"{c.case_id}: {c.status}")
    elif args.cmd == "ingest":
        res = p.process(SourceEvent.model_validate(json.load(open(args.file))))
        print(res.outcome, [f"{r.system}:{r.status}" for r in res.results])
    return 0


if __name__ == "__main__":
    sys.exit(main())
