"""Check: every section the front matter declares has a heading. Exit 0 or 1.

Rule: document.section_missing. Stdlib only; see _doc.py for the unit format.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _doc  # noqa: E402


def check(argv):
    document = _doc.load(_doc.unit_from_argv(argv))
    present = set(document.headings())
    missing = [name for name in document.meta["sections"] if name.strip().lower() not in present]
    for name in missing:
        _doc.emit("document.section_missing", f"required section '{name}' has no heading")
    return 1 if missing else 0


if __name__ == "__main__":
    _doc.run(check)
