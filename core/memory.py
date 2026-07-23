#!/usr/bin/env python3
"""memory.py, the four typed memory stores (plan §9).

| Store      | File                          | Discipline                        |
|------------|-------------------------------|-----------------------------------|
| Working    | state/now.md                  | current task; resets per session  |
| Episodic   | state/decisions.jsonl         | append-only audit trail           |
| Semantic   | state/facts.yaml              | consolidated on explicit commit   |
| Procedural | skills / adapter/checks/      | not managed here                  |

The episodic log is an audit trail, NOT a fact source, promote durable facts
explicitly with commit_fact (conflating the two is the classic failure mode).

CLI (for agent workflows):
  memory.py now --set <text> | --clear | --show
  memory.py decision --type <t> --decision <d> [--rationale --files --impact]
  memory.py fact --text <fact>
  memory.py show            # everything session_start would print
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import paths  # noqa: E402

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc


def _now_ts() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def now_path(root: Path) -> Path:
    return paths.state_dir(root) / "now.md"


def decisions_path(root: Path) -> Path:
    return paths.state_dir(root) / "decisions.jsonl"


def facts_path(root: Path) -> Path:
    return paths.state_dir(root) / "facts.yaml"


# --- working memory -----------------------------------------------------------
def read_now(root: Path) -> str:
    try:
        return now_path(root).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_now(root: Path, text: str) -> None:
    paths.atomic_write(now_path(root), text)


def clear_now(root: Path) -> None:
    now_path(root).unlink(missing_ok=True)


# --- episodic log (append-only, never overwritten) ----------------------------
def append_decision(root: Path, dtype: str, decision: str, rationale: str = "",
                    files: list | None = None, impact: str = "") -> dict:
    record = {"ts": _now_ts(), "type": dtype, "decision": decision,
              "rationale": rationale, "files": list(files or []), "impact": impact}
    p = decisions_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_decisions(root: Path, last: int | None = None) -> list[dict]:
    p = decisions_path(root)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if isinstance(rec, dict):
                out.append(rec)
        except json.JSONDecodeError:
            continue  # a corrupt line must not poison the whole trail
    return out[-last:] if last else out


# --- semantic facts (explicit consolidation) -----------------------------------
def read_facts(root: Path) -> list[dict]:
    p = facts_path(root)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    facts = data.get("facts")
    if not isinstance(facts, list):
        return []
    # facts.yaml is hand-editable; the natural YAML shorthand for an entry is
    # a bare string. Coerce instead of crashing, one such entry must never
    # blank the whole session context (every consumer assumes dicts).
    return [f if isinstance(f, dict) else {"text": str(f)} for f in facts]


def commit_fact(root: Path, text: str) -> bool:
    """Promote a durable fact. Returns False when it is already recorded."""
    text = text.strip()
    facts = read_facts(root)
    if any(f.get("text") == text for f in facts):
        return False
    facts.append({"at": _now_ts(), "text": text})
    paths.atomic_write(facts_path(root),
                       yaml.safe_dump({"facts": facts}, sort_keys=False,
                                      allow_unicode=True))
    return True


# --- CLI -----------------------------------------------------------------------
def cmd_show(root: Path) -> int:
    now = read_now(root).strip()
    print(f"now.md:\n  {now if now else '(empty)'}")
    facts = read_facts(root)
    print(f"facts ({len(facts)}):")
    for f in facts:
        print(f"  - {f.get('text', '')}")
    decisions = read_decisions(root, last=5)
    print(f"last decisions ({len(decisions)}):")
    for d in decisions:
        print(f"  - [{d.get('type', '?')}] {d.get('decision', '')}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("now")
    n.add_argument("--set")
    n.add_argument("--clear", action="store_true")
    n.add_argument("--show", action="store_true")
    n.add_argument("--root", default=None)

    d = sub.add_parser("decision")
    d.add_argument("--type", required=True, dest="dtype")
    d.add_argument("--decision", required=True)
    d.add_argument("--rationale", default="")
    d.add_argument("--files", default="")
    d.add_argument("--impact", default="")
    d.add_argument("--root", default=None)

    f = sub.add_parser("fact")
    f.add_argument("--text", required=True)
    f.add_argument("--root", default=None)

    s = sub.add_parser("show")
    s.add_argument("--root", default=None)

    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()

    if args.cmd == "now":
        if args.set is not None:
            write_now(root, args.set + ("\n" if not args.set.endswith("\n") else ""))
        elif args.clear:
            clear_now(root)
        else:
            print(read_now(root), end="")
        return 0
    if args.cmd == "decision":
        files = [x.strip() for x in args.files.split(",") if x.strip()]
        append_decision(root, args.dtype, args.decision, args.rationale,
                        files, args.impact)
        print("decision recorded.")
        return 0
    if args.cmd == "fact":
        added = commit_fact(root, args.text)
        print("fact committed." if added else "fact already recorded.")
        return 0
    return cmd_show(root)


if __name__ == "__main__":
    sys.exit(main())
