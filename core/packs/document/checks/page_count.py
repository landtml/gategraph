"""Check: the estimated page count is within the front matter's pages bounds.

The render is HTML and has no pages, so the count is an estimate: the body's
words divided by _doc.WORDS_PER_PAGE, rounded up, at least one page. Exit 0 or 1.

Rule: document.pages_out_of_bounds. Stdlib only.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _doc  # noqa: E402


def check(argv):
    document = _doc.load(_doc.unit_from_argv(argv))
    words = document.words()
    pages = max(1, math.ceil(words / _doc.WORDS_PER_PAGE))
    low, high = document.meta["pages"]
    if low <= pages <= high:
        return 0
    _doc.emit("document.pages_out_of_bounds",
              f"estimated {pages} page(s) from {words} words; the bounds are {low} to {high}")
    return 1


if __name__ == "__main__":
    _doc.run(check)
