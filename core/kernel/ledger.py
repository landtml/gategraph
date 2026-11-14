#!/usr/bin/env python3
"""The append-only failure ledger, `harness/state/failures.jsonl`, ported from
core/failures.py.

Every gate refusal, fatal lint at a gate attempt, escalation, and post-hoc
correction lands here as one JSON row.

Fixes in this port:
- B53: every row carries a short random `id`. Escalation labels refer to the
  row's id, so two escalations of one unit in the same second stay apart.
  Rows written by the old module have no id; they are still matched by `at`.
- B08: appends and label writes run under an exclusive file lock.

record never raises on an I/O failure: a refusal message must not turn into a
crash because the ledger is broken. Standard library only.

CLI:
  python3 core/kernel/ledger.py [--root R] record --kind correction --note <what> [--unit U]
  python3 core/kernel/ledger.py [--root R] show [--last N]
  python3 core/kernel/ledger.py [--root R] report
  python3 core/kernel/ledger.py [--root R] escalations
  python3 core/kernel/ledger.py [--root R] label (--id ID | --at TS [--unit U]) --worthy yes|no [--note ..]
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

LOCK_TIMEOUT_SECONDS = 30.0


def _now_ts() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def failures_path(root: Path) -> Path:
    return Path(root) / "harness" / "state" / "failures.jsonl"


@contextlib.contextmanager
def _locked(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Exclusive lock on a sibling `<name>.lock` file, waiting up to `timeout`."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path.with_name(path.name + ".lock"), "a+", encoding="utf-8")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out waiting for the lock on {path.name}") from None
                time.sleep(0.02)
        yield
    finally:
        fh.close()  # closing the descriptor releases the flock


def _append_row(p: Path, row: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def record(root: Path, kind: str, **fields) -> dict:
    """Append one failure row and return it. Never raises on I/O errors."""
    row = {"at": _now_ts(), "kind": kind, **fields}
    row.setdefault("id", secrets.token_hex(4))
    try:
        p = failures_path(root)
        with _locked(p):
            _append_row(p, row)
    except OSError:
        pass
    return row


def read_ledger(root: Path, last: int | None = None) -> list[dict]:
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


def _key(row: dict) -> str | None:
    """What an escalation label refers to: the row id, or its timestamp for
    rows written before ids existed."""
    return row.get("id") or row.get("at")


def label(root: Path, worthy: bool, *, ref: str | None = None,
          at: str | None = None, unit: str | None = None,
          note: str | None = None) -> dict:
    """Record a human verdict on one escalation row.

    Select the row by `ref` (its id), or by `at` (its timestamp, with `unit`
    to disambiguate). Raises LookupError when no escalation matches, or when
    the timestamp selects more than one row."""
    p = failures_path(root)
    with _locked(p):
        rows = read_ledger(root)
        escalations = [r for r in rows if r.get("kind") == "escalation"]
        matches = [r for r in escalations if ref is not None and r.get("id") == ref]
        if not matches and ref is not None:
            matches = [r for r in escalations
                       if r.get("at") == ref and (unit is None or r.get("unit") == unit)]
        if not matches and at is not None:
            matches = [r for r in escalations
                       if r.get("at") == at and (unit is None or r.get("unit") == unit)]
        if not matches:
            raise LookupError("no escalation row matches; list them with: "
                              "ledger.py escalations")
        if len(matches) > 1:
            units = ", ".join(str(m.get("unit")) for m in matches)
            raise LookupError(f"ambiguous: {len(matches)} escalations share that "
                              f"timestamp (units: {units}); select one with --id")
        target = matches[0]
        row = {"at": _now_ts(), "kind": "escalation_label", "ref": _key(target),
               "ref_unit": target.get("unit"), "worthy": bool(worthy), "note": note,
               "id": secrets.token_hex(4)}
        try:
            _append_row(p, row)
        except OSError as exc:
            raise OSError(f"could not write the label: {exc}") from exc
        return row


def escalation_tally(rows: list[dict]) -> str:
    """Every escalation row with its latest label (latest wins), and precision
    once at least one event is labeled. The T3 table needs at least 20 labeled
    real events."""
    events = [r for r in rows if r.get("kind") == "escalation"]
    if not events:
        return "no escalation rows yet."
    label_rows = [r for r in rows
                  if r.get("kind") == "escalation_label" and r.get("ref")]

    def latest_label(event: dict) -> dict | None:
        lab = None
        for r in label_rows:  # file order is append order; latest wins
            if r["ref"] == _key(event) and r.get("ref_unit") in (None, event.get("unit")):
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
        lines.append(f"  {e.get('id') or e.get('at')}  {e.get('unit') or '?':<12} "
                     f"{verdict:<10} {e.get('note') or ''}")
    if labeled:
        worthy = sum(1 for lab in labeled if lab.get("worthy"))
        lines.append(f"precision: {worthy}/{len(labeled)} labeled "
                     f"human-worthy (T3 needs at least 20 labeled real events)")
    else:
        lines.append("no labels yet, label events with: "
                     "ledger.py label --id <id> --worthy yes|no")
    return "\n".join(lines)


def report(rows: list[dict]) -> str:
    """T2 instrument, ported from core/failures.py report: the ledger triaged, rows
    grouped by kind (with a per-gate breakdown), a month trend and the busiest units.
    Recurring rows are the signal that a failure class deserves a new check."""
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
    span = (f"{min(str(r.get('at', '')) for r in rows)[:10]} → "
            f"{max(str(r.get('at', '')) for r in rows)[:10]}")
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


def _build_parser() -> argparse.ArgumentParser:
    # --root is declared once, here. SUPPRESS keeps a subparser's absent
    # --root from overwriting a value given before the subcommand (B29).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", parents=[common])
    r.add_argument("--kind", default="correction")
    r.add_argument("--unit", default=None)
    r.add_argument("--note", required=True)

    s = sub.add_parser("show", parents=[common])
    s.add_argument("--last", type=int, default=20)

    sub.add_parser("escalations", parents=[common])
    sub.add_parser("report", parents=[common])

    lab = sub.add_parser("label", parents=[common])
    sel = lab.add_mutually_exclusive_group(required=True)
    sel.add_argument("--id", dest="ref", help="the escalation row's id")
    sel.add_argument("--at", help="the escalation row's timestamp")
    lab.add_argument("--unit", default=None, help="disambiguates same-second rows")
    lab.add_argument("--worthy", required=True, choices=("yes", "no"))
    lab.add_argument("--note", default=None)
    return p


def _find_root(start: Path | None = None) -> Path:
    here = Path(start or os.getcwd()).resolve()
    for d in (here, *here.parents):
        if (d / "harness" / "adapter" / "adapter.yaml").is_file():
            return d
    raise SystemExit("no project root found (harness/adapter/adapter.yaml); "
                     "pass --root")


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve() if hasattr(args, "root") else _find_root()
    if args.cmd == "record":
        row = record(root, args.kind, unit=args.unit, note=args.note)
        print(f"recorded: {row['kind']} ({row['at']}, id {row['id']})")
        return 0
    if args.cmd == "escalations":
        print(escalation_tally(read_ledger(root)))
        return 0
    if args.cmd == "report":
        print(report(read_ledger(root)))
        return 0
    if args.cmd == "label":
        try:
            row = label(root, args.worthy == "yes", ref=args.ref, at=args.at,
                        unit=args.unit, note=args.note)
        except (LookupError, OSError, TimeoutError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"labeled {row['ref']} ({row['ref_unit']}): "
              f"{'worthy' if row['worthy'] else 'not-worthy'}")
        return 0
    for rec in read_ledger(root, last=args.last):
        extra = {k: v for k, v in rec.items() if k not in ("at", "kind")}
        print(f"{rec['at']}  {rec['kind']:<14} {json.dumps(extra, ensure_ascii=False)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
