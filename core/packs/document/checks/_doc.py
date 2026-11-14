"""Shared reading for the document pack's checks. Standard library only.

A unit holds ``document.md``: a front matter block, then Markdown.

    ---
    title: "Synthetic throughput note"
    sections: ["Summary", "Method", "Results", "Limitations"]
    pages: [1, 2]
    ---

Each front matter line is ``key: <JSON value>``. ``sections`` lists the headings
the document must carry; ``pages`` gives the inclusive page bounds.

Check exit codes (kernel/checks.py): 0 pass, 1 fatal. Findings go to standard
output, one JSON object per line, which the kernel reads as Finding records.
"""
import json
import re
import sys
from pathlib import Path

SOURCE = "document.md"
WORDS_PER_PAGE = 450
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
FENCE = "```"


class Problem(Exception):
    """A fatal problem with the unit. ``rule`` names it for the gate report."""

    def __init__(self, rule, message, line=None):
        super().__init__(message)
        self.rule = rule
        self.message = message
        self.line = line


class Document:
    def __init__(self, meta, lines, body_start):
        self.meta = meta
        self.lines = lines          # every line of the file, front matter included
        self.body_start = body_start  # index of the first body line

    def body(self):
        """(line number, text) for each body line, numbered from 1 in the file."""
        return [(index + 1, self.lines[index]) for index in range(self.body_start, len(self.lines))]

    def headings(self):
        """Heading texts, lower-cased, in order."""
        found = []
        for _, line in self.body():
            match = HEADING.match(line.strip())
            if match:
                found.append(match.group(1).strip().lower())
        return found

    def words(self):
        return sum(len(line.split()) for _, line in self.body())


def emit(rule, message, line=None, severity="fatal"):
    """Print one finding as a JSON line on standard output."""
    obj = {"rule": rule, "severity": severity, "path": SOURCE, "line": line, "message": message}
    print(json.dumps(obj, sort_keys=True))


def unit_from_argv(argv):
    if len(argv) != 1:
        raise Problem("document.usage", "usage: <check> <unit directory>")
    return argv[0]


def load(unit):
    """Read the unit's document, or raise Problem."""
    path = Path(unit) / SOURCE
    if not path.is_file():
        raise Problem("document.source_missing", f"{SOURCE} is missing from the unit")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise Problem("document.unreadable", f"{SOURCE} is not UTF-8 text: {exc}") from None
    if not lines or lines[0].strip() != "---":
        raise Problem("document.front_matter", "the document must open with a --- front matter block", line=1)
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise Problem("document.front_matter", "the front matter block is not closed by ---", line=1) from None
    meta = {}
    for index in range(1, end):
        raw = lines[index]
        if not raw.strip():
            continue
        key, sep, value = raw.partition(":")
        if not sep or not key.strip():
            raise Problem("document.front_matter", "expected a 'key: JSON value' line", line=index + 1)
        try:
            meta[key.strip()] = json.loads(value)
        except ValueError:
            raise Problem("document.front_matter", f"{key.strip()} is not a JSON value", line=index + 1) from None
    for key in ("title", "sections", "pages"):
        if key not in meta:
            raise Problem("document.front_matter", f"front matter has no '{key}' key", line=1)
    if not isinstance(meta["title"], str):
        raise Problem("document.front_matter", "title must be a string", line=1)
    if not isinstance(meta["sections"], list) or not all(isinstance(s, str) for s in meta["sections"]):
        raise Problem("document.front_matter", "sections must be a list of strings", line=1)
    pages = meta["pages"]
    if (not isinstance(pages, list) or len(pages) != 2
            or not all(type(p) is int for p in pages) or not 1 <= pages[0] <= pages[1]):
        raise Problem("document.front_matter", "pages must be [min, max] integers with 1 <= min <= max", line=1)
    return Document(meta, lines, end + 1)


def run(check):
    """Call ``check(argv)`` and turn a Problem into one finding and exit 1."""
    try:
        code = check(sys.argv[1:])
    except Problem as problem:
        emit(problem.rule, problem.message, problem.line)
        code = 1
    sys.exit(code)
