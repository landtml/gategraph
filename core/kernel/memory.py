#!/usr/bin/env python3
"""Typed memory stores (plan section 9), ported from core/memory.py.

| Store      | File                          | Discipline                        |
|------------|-------------------------------|-----------------------------------|
| Working    | harness/state/now.md          | current task; resets per session  |
| Episodic   | harness/state/decisions.jsonl | append-only audit trail           |
| Semantic   | harness/state/facts.yaml      | consolidated on explicit commit   |
| Procedural | skills / adapter/checks/      | not managed here                  |

The episodic log is an audit trail, NOT a fact source. Promote durable facts
explicitly with commit_fact.

Fixes in this port:
- B08: a facts.yaml that does not parse is never rewritten. read_facts raises
  FactsCorrupt; commit_fact moves the file aside as facts.yaml.corrupt (or
  facts.N.corrupt when that name is taken), commits nothing and raises.
- B08: every write is a read-modify-write under an exclusive file lock.
- B29: --root is declared once, on a parent parser shared by every subparser.

Standard library and PyYAML only. Runnable as a script:
  python3 core/kernel/memory.py [--root R] now --set <text> | --clear | --show
  python3 core/kernel/memory.py [--root R] decision --type <t> --decision <d> [...]
  python3 core/kernel/memory.py [--root R] fact --text <fact>
  python3 core/kernel/memory.py [--root R] show
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

LOCK_TIMEOUT_SECONDS = 30.0


class FactsCorrupt(ValueError):
    """facts.yaml exists but cannot be read as the facts shape."""


def _now_ts() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _state_dir(root: Path) -> Path:
    return Path(root) / "harness" / "state"


def now_path(root: Path) -> Path:
    return _state_dir(root) / "now.md"


def decisions_path(root: Path) -> Path:
    return _state_dir(root) / "decisions.jsonl"


def facts_path(root: Path) -> Path:
    return _state_dir(root) / "facts.yaml"


# --- primitives: atomic write and the file lock ------------------------------
def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix="~")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def _locked(path: Path, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Exclusive lock on a sibling `<name>.lock` file. Waits for the holder
    up to `timeout` seconds, then raises TimeoutError (an OSError)."""
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


# --- working memory -----------------------------------------------------------
def read_now(root: Path) -> str:
    try:
        return now_path(root).read_text(encoding="utf-8")
    except OSError:
        return ""


def write_now(root: Path, text: str) -> None:
    p = now_path(root)
    with _locked(p):
        _atomic_write(p, text)


def clear_now(root: Path) -> None:
    p = now_path(root)
    with _locked(p):
        p.unlink(missing_ok=True)


# --- episodic log (append-only, never overwritten) ----------------------------
def append_decision(root: Path, dtype: str, decision: str, rationale: str = "",
                    files: list | None = None, impact: str = "") -> dict:
    record = {"ts": _now_ts(), "type": dtype, "decision": decision,
              "rationale": rationale, "files": list(files or []), "impact": impact}
    p = decisions_path(root)
    with _locked(p):
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
def _parse_facts(text: str) -> list[dict]:
    """Facts from facts.yaml text. Raises FactsCorrupt when the text is not
    YAML of the form `facts: [...]` (an empty file or `facts:` is empty)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FactsCorrupt(str(exc)) from exc
    if data is None or data == {}:
        return []
    if not isinstance(data, dict):
        raise FactsCorrupt("top level is not a mapping")
    if "facts" not in data:
        # Any other content would be lost by a rewrite, so refuse it.
        raise FactsCorrupt("no 'facts' key")
    facts = data["facts"]
    if facts is None:
        return []
    if not isinstance(facts, list):
        raise FactsCorrupt("'facts' is not a list")
    # facts.yaml is hand-editable; the natural YAML shorthand for an entry is
    # a bare string. Coerce instead of crashing, one such entry must never
    # blank the whole session context (every consumer assumes dicts).
    return [f if isinstance(f, dict) else {"text": str(f)} for f in facts]


def read_facts(root: Path) -> list[dict]:
    p = facts_path(root)
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise FactsCorrupt(str(exc)) from exc
    return _parse_facts(text)


def _move_aside(p: Path) -> Path:
    """Move an unreadable facts file to the first free `facts.yaml.corrupt`
    (or `facts.N.corrupt`). Existing aside copies are never overwritten."""
    state = p.parent
    variant = state / (p.name + ".corrupt")
    n = 2
    while variant.exists():
        variant = state / f"facts.{n}.corrupt"
        n += 1
    os.replace(p, variant)
    return variant


def commit_fact(root: Path, text: str) -> bool:
    """Promote a durable fact. Returns False when it is already recorded.

    Raises FactsCorrupt, after moving the unreadable file aside, when
    facts.yaml cannot be parsed. Nothing is written in that case."""
    text = text.strip()
    p = facts_path(root)
    with _locked(p):
        try:
            facts = read_facts(root)
        except FactsCorrupt as exc:
            aside = _move_aside(p) if p.exists() else None
            where = aside.name if aside else p.name
            raise FactsCorrupt(
                f"facts.yaml could not be parsed ({exc}). It was moved to "
                f"{where} and nothing was committed. Restore the entries you "
                f"need into facts.yaml from that file, then run the fact "
                f"command again.") from exc
        if any(f.get("text") == text for f in facts):
            return False
        facts.append({"at": _now_ts(), "text": text})
        _atomic_write(p, yaml.safe_dump({"facts": facts}, sort_keys=False,
                                        allow_unicode=True))
        return True


# --- CLI -----------------------------------------------------------------------
def _find_root(start: Path | None = None) -> Path:
    """The directory holding harness/adapter/adapter.yaml, walking up."""
    here = Path(start or os.getcwd()).resolve()
    for d in (here, *here.parents):
        if (d / "harness" / "adapter" / "adapter.yaml").is_file():
            return d
    raise SystemExit("no project root found (harness/adapter/adapter.yaml); "
                     "pass --root")


def cmd_show(root: Path) -> int:
    now = read_now(root).strip()
    print(f"now.md:\n  {now if now else '(empty)'}")
    try:
        facts = read_facts(root)
    except FactsCorrupt as exc:
        print(f"facts: unreadable ({exc}); see the fact command for recovery",
              file=sys.stderr)
        return 1
    print(f"facts ({len(facts)}):")
    for f in facts:
        print(f"  - {f.get('text', '')}")
    decisions = read_decisions(root, last=5)
    print(f"last decisions ({len(decisions)}):")
    for d in decisions:
        print(f"  - [{d.get('type', '?')}] {d.get('decision', '')}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    # --root is declared once, here. SUPPRESS keeps a subparser's absent
    # --root from overwriting a value given before the subcommand (B29).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=argparse.SUPPRESS)

    p = argparse.ArgumentParser(description=__doc__, parents=[common],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("now", parents=[common])
    n.add_argument("--set")
    n.add_argument("--clear", action="store_true")
    n.add_argument("--show", action="store_true")

    d = sub.add_parser("decision", parents=[common])
    d.add_argument("--type", required=True, dest="dtype")
    d.add_argument("--decision", required=True)
    d.add_argument("--rationale", default="")
    d.add_argument("--files", default="")
    d.add_argument("--impact", default="")

    f = sub.add_parser("fact", parents=[common])
    f.add_argument("--text", required=True)

    sub.add_parser("show", parents=[common])
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.root).resolve() if hasattr(args, "root") else _find_root()

    try:
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
    except (FactsCorrupt, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
