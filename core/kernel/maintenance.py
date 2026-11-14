"""Maintenance rituals and their receipt log.

`status` prints what is due on the monthly cadence; `done` appends a receipt to
the state log, which later proves the rhythm happened. The checklist informs;
it never gates (exit 0 even when everything is due).

The flags-verify ritual of the legacy module is not ported: the Claude CLI pin
check moves to the claude_cli port (plan section 2.x, module table).

Usage: python3 -m core.kernel.maintenance [--root DIR] [status]
       python3 -m core.kernel.maintenance [--root DIR] done <ritual> [--note <what>]
Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from core.kernel import paths

DUE_DAYS = 30
LOG_NAME = "maintenance.jsonl"
RITUALS = {
    "corpus-rotation": "rotate ~10% of eval cases from ledger rows (docs/EVAL.md)",
    "ledger-triage": "review the failure report; recurring rows become checks",
}


def log_path(root) -> Path:
    return paths.state_dir(root) / LOG_NAME


def read_log(root) -> list[dict]:
    p = log_path(root)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn write must not poison the whole log
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def record(root, ritual: str, note: str | None = None,
           now: datetime | None = None) -> dict:
    if ritual not in RITUALS:
        raise ValueError(f"unknown ritual {ritual!r}, one of: {', '.join(RITUALS)}")
    stamp = (now or datetime.now()).replace(microsecond=0).isoformat()
    row = {"at": stamp, "ritual": ritual, "note": note}
    p = log_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def status_text(root, now: datetime | None = None) -> str:
    now = now or datetime.now()
    last: dict[str, str] = {}
    for r in read_log(root):
        if r.get("ritual") in RITUALS and r.get("at"):
            last[r["ritual"]] = r["at"]  # append order: latest wins
    lines = [f"maintenance: rituals for {root} (cadence {DUE_DAYS}d)"]
    n_due = 0
    for name in RITUALS:
        at = last.get(name)
        if at is None:
            is_due, when = True, "never done"
        else:
            days = (now - datetime.fromisoformat(at)).days
            is_due = days >= DUE_DAYS
            when = f"done {at[:10]} ({days}d ago)"
        n_due += is_due
        lines.append(f"  {'DUE' if is_due else 'ok':<4} {name:<17} "
                     f"{when}: {RITUALS[name]}")
    lines.append(f"due: {n_due}, receipts: {paths.state_dir(root).name}/{LOG_NAME}")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Maintenance rituals and receipts.")
    p.add_argument("--root", default=None)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    d = sub.add_parser("done")
    d.add_argument("ritual")
    d.add_argument("--note", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_root()
    if args.cmd == "done":
        if args.ritual not in RITUALS:
            print(f"error: unknown ritual {args.ritual!r}, one of: "
                  f"{', '.join(RITUALS)}", file=sys.stderr)
            return 1
        row = record(root, args.ritual, note=args.note)
        print(f"recorded: {row['ritual']} ({row['at']})")
        return 0
    print(status_text(root))
    return 0


if __name__ == "__main__":  # run from the repository root
    sys.exit(main())
