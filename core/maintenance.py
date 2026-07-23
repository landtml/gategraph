#!/usr/bin/env python3
"""maintenance.py, the T13 ritual checklist and its receipt log.

`status` prints what's due on the ~monthly maintenance cadence; `done`
appends a receipt to `harness/state/maintenance.jsonl`, the log that
later proves the rhythm happened. The checklist
informs; it never gates (exit 0 even when everything is due).

Rituals (cadence 30 days):
  corpus-rotation  rotate ~10% of eval cases from ledger rows (docs/EVAL.md)
  ledger-triage    review `failures.py report`; recurring rows become checks
  flags-verify     re-verify the core/loop/FLAGS.md CLI pin (loop projects)

Usage: maintenance.py [--root DIR] [status]
       maintenance.py [--root DIR] done <ritual> [--note <what>]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, paths  # noqa: E402

DUE_DAYS = 30
RITUALS = {
    "corpus-rotation": "rotate ~10% of eval cases from ledger rows (docs/EVAL.md)",
    "ledger-triage": "review `failures.py report`; recurring rows become checks",
    "flags-verify": "re-verify the core/loop/FLAGS.md CLI pin (verify-flags --live)",
}


def log_path(root: Path) -> Path:
    return paths.state_dir(root) / "maintenance.jsonl"


def read_log(root: Path) -> "list[dict]":
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


def record(root: Path, ritual: str, note: "str | None" = None) -> dict:
    row = {"at": datetime.now().replace(microsecond=0).isoformat(),
           "ritual": ritual, "note": note}
    p = log_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def applicable(root: Path) -> "list[str]":
    names = ["corpus-rotation", "ledger-triage"]
    try:
        loop_on = bool(config.load(root).loop.get("enabled"))
    except Exception:
        loop_on = True  # can't tell, show the full checklist
    if loop_on:
        names.append("flags-verify")
    return names


def status_text(root: Path, now: "datetime | None" = None) -> str:
    now = now or datetime.now()
    last: dict = {}
    for r in read_log(root):
        if r.get("ritual") in RITUALS and r.get("at"):
            last[r["ritual"]] = r["at"]  # append order, latest wins
    lines = [f"maintenance: rituals for {root} (cadence {DUE_DAYS}d)"]
    n_due = 0
    for name in applicable(root):
        at = last.get(name)
        if at is None:
            is_due, when = True, "never done"
        else:
            days = (now - datetime.fromisoformat(at)).days
            is_due = days >= DUE_DAYS
            when = f"done {at[:10]} ({days}d ago)"
        n_due += is_due
        lines.append(f"  {'DUE' if is_due else 'ok':<4}{name:<17} "
                     f"{when}: {RITUALS[name]}")
    lines.append(f"due: {n_due}, receipts: harness/state/maintenance.jsonl")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status")
    d = sub.add_parser("done")
    d.add_argument("ritual")
    d.add_argument("--note", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()
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


if __name__ == "__main__":
    sys.exit(main())
