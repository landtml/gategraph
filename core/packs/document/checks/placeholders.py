"""Check: the body carries no placeholder text. Exit 0 or 1.

Rule: document.placeholder, one finding per matching line. Stdlib only.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _doc  # noqa: E402

PATTERNS = (
    (re.compile(r"\b(?:TODO|TBD|FIXME|XXX)\b"), "marker"),
    (re.compile(r"lorem ipsum", re.IGNORECASE), "filler"),
    (re.compile(r"\[(?:insert|placeholder|tbd)\b[^\]]*\]", re.IGNORECASE), "bracketed"),
    (re.compile(r"\{\{[^}]*\}\}"), "template"),
)


def check(argv):
    document = _doc.load(_doc.unit_from_argv(argv))
    hits = 0
    for number, line in document.body():
        for pattern, kind in PATTERNS:
            match = pattern.search(line)
            if match:
                hits += 1
                _doc.emit("document.placeholder",
                          f"{kind} placeholder text {match.group(0)!r}", line=number)
                break
    return 1 if hits else 0


if __name__ == "__main__":
    _doc.run(check)
