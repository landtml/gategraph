#!/usr/bin/env python3
"""failures.py, the append-only failure ledger, `harness/state/failures.jsonl` (§13).

Every gate refusal, fatal lint at a gate attempt, escalation, and post-hoc
"this shipped wrong" correction lands here as one JSON row. Recurring rows are
the signal that a failure class deserves a new adapter check (and, if generic,
a `gatebench upstream` to core). The eval runner appends its failures too;
this ledger is the mechanism by which the harness grows with the project.

CLI (post-hoc corrections, the one kind no tool can auto-record):
  python3 harness/core/failures.py record --kind correction --unit <slug> --note <what>
  python3 harness/core/failures.py show [--last N]
  python3 harness/core/failures.py report            # T2: triage, kinds/gates/trend
  python3 harness/core/failures.py escalations       # T3: tally + precision
  python3 harness/core/failures.py label --at <ts> --worthy yes|no [--note ..]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import paths  # noqa: E402


def failures_path(root: Path) -> Path:
    return paths.state_dir(root) / "failures.jsonl"


def record(root: Path, kind: str, **fields) -> dict:
    """Append one failure row. Never raises, a broken ledger must not turn a
    refusal message into a crash (the refusal is the product; the row is telemetry)."""
    row = {"at": datetime.now().replace(microsecond=0).isoformat(), "kind": kind, **fields}
    try:
        p = failures_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return row


def read(root: Path, last: "int | None" = None) -> "list[dict]":
    p = failures_path(root)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn write must not poison the whole ledger
        if isinstance(rec, dict):
            rows.append(rec)
    return rows[-last:] if last else rows


def report(rows: "list[dict]") -> str:
    """T2 instrument: the ledger triaged, rows grouped by kind (with a
    per-gate breakdown), a month trend, and the busiest units. Recurring
    rows here are the signal that a failure class deserves a new check."""
    if not rows:
        return "failures.jsonl: empty, nothing recurring yet."
    from collections import Counter
    kinds = Counter(r.get("kind", "?") for r in rows)
    gates_by_kind: dict = {}
    for r in rows:
        if r.get("gate"):
            gates_by_kind.setdefault(r.get("kind", "?"), Counter())[r["gate"]] += 1
    months = Counter(str(r.get("at", "?"))[:7] for r in rows)
    units = Counter(r["unit"] for r in rows if r.get("unit"))
    span = f"{min(str(r.get('at', '')) for r in rows)[:10]} → " \
           f"{max(str(r.get('at', '')) for r in rows)[:10]}"
    lines = [f"failures.jsonl: {len(rows)} rows, {span}", "by kind:"]
    for kind, n in kinds.most_common():
        gate_part = ""
        if kind in gates_by_kind:
            gate_part = "  (" + ", ".join(
                f"{g} {c}" for g, c in gates_by_kind[kind].most_common()) + ")"
        lines.append(f"  {kind:<18} {n}{gate_part}")
    lines.append("by month:")
    for month in sorted(months):
        lines.append(f"  {month}  {months[month]}")
    if units:
        lines.append("top units: " + ", ".join(
            f"{u} ({c})" for u, c in units.most_common(5)))
    return "\n".join(lines)


def escalation_tally(rows: "list[dict]") -> str:
    """T3 instrument: every `kind: escalation` row joined with its latest
    `escalation_label` (ref == the escalation's `at`; latest wins, same
    convention as the registry's verdicts). Precision appears once at
    least one event is labeled; the T3 table needs ≥20 labeled REAL events."""
    events = [r for r in rows if r.get("kind") == "escalation"]
    if not events:
        return "no escalation rows yet."
    label_rows = [r for r in rows
                  if r.get("kind") == "escalation_label" and r.get("ref")]

    def latest_label(event: dict) -> "dict | None":
        lab = None
        for r in label_rows:  # file order is append order; latest wins
            if r["ref"] == event.get("at") and \
                    r.get("ref_unit") in (None, event.get("unit")):
                lab = r
        return lab

    lines = [f"escalations: {len(events)}"]
    labeled = []
    for e in events:
        lab = latest_label(e)
        if lab is not None:
            labeled.append(lab)
        verdict = ("unlabeled" if lab is None else
                   ("worthy" if lab.get("worthy") else "not-worthy"))
        lines.append(f"  {e.get('at')}  {e.get('unit') or '?':<12} "
                     f"{verdict:<10} {e.get('note') or ''}")
    if labeled:
        worthy = sum(1 for lab in labeled if lab.get("worthy"))
        lines.append(f"precision: {worthy}/{len(labeled)} labeled "
                     f"human-worthy (T3 needs ≥20 labeled real events)")
    else:
        lines.append("no labels yet, label events with: "
                     "failures.py label --at <ts> --worthy yes|no")
    return "\n".join(lines)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--kind", default="correction")
    r.add_argument("--unit", default=None)
    r.add_argument("--note", required=True)
    s = sub.add_parser("show")
    s.add_argument("--last", type=int, default=20)
    sub.add_parser("report")
    sub.add_parser("escalations")
    lab = sub.add_parser("label")
    lab.add_argument("--at", required=True,
                     help="the escalation row's `at` timestamp (see escalations)")
    lab.add_argument("--unit", default=None,
                     help="disambiguates same-second escalations")
    lab.add_argument("--worthy", required=True, choices=("yes", "no"))
    lab.add_argument("--note", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()
    if args.cmd == "record":
        row = record(root, args.kind, unit=args.unit, note=args.note)
        print(f"recorded: {row['kind']} ({row['at']})")
        return 0
    if args.cmd == "report":
        print(report(read(root)))
        return 0
    if args.cmd == "escalations":
        print(escalation_tally(read(root)))
        return 0
    if args.cmd == "label":
        matches = [r for r in read(root)
                   if r.get("kind") == "escalation" and r.get("at") == args.at
                   and (args.unit is None or r.get("unit") == args.unit)]
        if not matches:
            print(f"error: no escalation row with at={args.at!r}, list them "
                  f"with: failures.py escalations", file=sys.stderr)
            return 1
        if len(matches) > 1:
            units = ", ".join(str(m.get("unit")) for m in matches)
            print(f"error: ambiguous, {len(matches)} escalations share "
                  f"at={args.at!r} (units: {units}); pass --unit",
                  file=sys.stderr)
            return 1
        row = record(root, "escalation_label", ref=args.at,
                     ref_unit=matches[0].get("unit"),
                     worthy=(args.worthy == "yes"), note=args.note)
        print(f"labeled {args.at} ({row['ref_unit']}): "
              f"{'worthy' if row['worthy'] else 'not-worthy'}")
        return 0
    for rec in read(root, last=args.last):
        extra = {k: v for k, v in rec.items() if k not in ("at", "kind")}
        print(f"{rec['at']}  {rec['kind']:<14} {json.dumps(extra, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
