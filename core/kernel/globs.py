"""One glob dialect for the whole kernel (B34, B41).

Rules, for repo-relative POSIX paths:

- ``*`` matches any run of characters inside one path segment. It never
  crosses ``/``. Leading dots are not special.
- ``?`` matches one character other than ``/``.
- ``[...]`` and ``[!...]`` match one character from a set, inside a segment.
- A whole segment ``**`` in the middle matches zero or more directories, so
  ``units/**/*.tex`` matches ``units/x.tex`` and ``units/a/b/x.tex``.
- A trailing ``**`` matches one or more path segments under the prefix, so
  ``a/**`` matches ``a/x`` and ``a/x/y`` but not ``a``.
- ``**`` inside a longer segment behaves like ``*``.

Matching is whole-path and case-sensitive. The path is normalized with
PurePosixPath (``./`` and doubled slashes disappear). Standard library only.
"""
from __future__ import annotations

import functools
import re
from pathlib import PurePosixPath

_DIR_STAR = r"(?:[^/]+/)*"   # zero or more directories, separator included
_TAIL_STAR = r".+"           # trailing ** : at least one character


def _segment_regex(seg: str) -> str:
    out: list[str] = []
    i, n = 0, len(seg)
    while i < n:
        ch = seg[i]
        if ch == "*":
            out.append("[^/]*")
            while i + 1 < n and seg[i + 1] == "*":
                i += 1
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            close = seg.find("]", i + 2 if i + 1 < n and seg[i + 1] in "!^" else i + 1)
            if close == -1:
                out.append(re.escape(ch))
            else:
                body = seg[i + 1:close]
                neg = body[:1] in ("!", "^")
                if neg:
                    body = body[1:]
                out.append("[" + ("^/" if neg else "") + body.replace("\\", "\\\\") + "]")
                i = close
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern:
    segments = [s for s in pattern.split("/") if s not in ("", ".")]
    if not segments:
        return re.compile(r"(?!)")  # matches nothing
    parts: list[str] = []
    last = len(segments) - 1
    for idx, seg in enumerate(segments):
        if seg == "**":
            if idx == last:
                piece = _TAIL_STAR
            else:
                piece = _DIR_STAR
        else:
            piece = _segment_regex(seg)
        if idx > 0 and not (segments[idx - 1] == "**" and idx - 1 != last):
            parts.append("/")
        parts.append(piece)
    return re.compile("".join(parts), re.DOTALL)


def normalize_path(path: str) -> str:
    """Repo-relative POSIX form used for matching."""
    return PurePosixPath(path).as_posix()


def glob_match(pattern: str, path: str) -> bool:
    """True if the whole ``path`` matches ``pattern`` under the kernel dialect."""
    return _compile(pattern).fullmatch(normalize_path(path)) is not None
