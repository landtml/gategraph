#!/usr/bin/env python3
"""verify_claims.py: the research pack's claim check (port of seed/checks/verify_claims.py).

The seed's bug, kept fixed here (F6): a check that verifies only the claim
lines that PARSE lets a typo'd or deleted claim line skip verification and
still earn the gate. Absence must fail, not skip.

Memo convention (the report is a memo):

    claim: <name> = <number> | source: <path>

``<path>`` is relative to ``--unit``, the unit directory (a cited source lives
under the unit's ``sources/``). Every line that starts with ``claim:`` (after
indentation or a list bullet) is a claim line and must parse; a line that does
not parse is a finding, never a silent skip.

Modes:
  sources  every claim cites a source, the cited file exists, the memo carries
           at least one claim, and no claim name is stated twice
  values   everything ``sources`` checks, and each claimed number appears in the
           cited source (within --tolerance)

Other options, kept from the seed (wire them in an adapter check, one
--require per mandatory claim, to enforce a named set of claims):
  --require NAME       the claim must be present in every memo
  --actual NAME=VALUE  a recomputed value to compare with the memo's claim

Exit codes (the check contract): 0 pass; 1 fatal. Usage errors are fatal too
(argparse would exit 2, which is the warn code). On failure, stdout carries one
JSON finding per line: {"rule", "severity", "path", "line", "message"}.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VARIANT = re.compile(r"^\s*(?:[-*]\s+)?claim:", re.IGNORECASE)
PARSE = re.compile(
    r"^\s*(?:[-*]\s+)?claim:\s*(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*"
    r"(?:\|\s*source:\s*(\S+))?\s*$",
    re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w]|\.\d)")
MODES = ("sources", "values")


def emit(rule: str, message: str, path: str | None = None, line: int | None = None) -> None:
    """One JSON finding on stdout, always fatal."""
    print(json.dumps({"rule": rule, "severity": "fatal", "path": path,
                      "line": line, "message": message}))


class _Usage(argparse.ArgumentParser):
    def error(self, message):  # argparse would exit 2 (warn); a bad call is fatal
        emit("claims.usage", f"usage error: {message}")
        sys.exit(1)


def parse_memo(text: str):
    """(claims by name -> [(value, source, line)], problems [(rule, line, message)])."""
    claims: dict[str, list] = {}
    problems: list[tuple[str, int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not VARIANT.match(line):
            continue
        match = PARSE.match(line)
        if match is None:
            problems.append(("claims.malformed", lineno,
                             "claim line does not parse; expected "
                             "'claim: <name> = <number> | source: <path>'"))
            continue
        name, value, source = match.group(1), float(match.group(2)), match.group(3)
        claims.setdefault(name, []).append((value, source, lineno))
    return claims, problems


def numbers_in(text: str) -> list[float]:
    return [float(token) for token in NUMBER.findall(text)]


def main(argv=None) -> int:
    p = _Usage(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("memos", nargs="*",
                   help="the report file(s) making the claims, relative to the working directory")
    p.add_argument("--unit", default=".", metavar="DIR",
                   help="the unit directory; cited sources are relative to it")
    p.add_argument("--mode", choices=MODES, default="values")
    p.add_argument("--require", action="append", default=[], metavar="NAME",
                   help="claim that MUST be present (repeatable)")
    p.add_argument("--actual", action="append", default=[], metavar="NAME=VALUE",
                   help="ground-truth value to verify a claim against (repeatable)")
    p.add_argument("--tolerance", type=float, default=1e-6)
    args = p.parse_args(argv)

    if not args.memos:
        emit("claims.no_input", "no report matched: nothing to verify")
        return 1

    unit = Path(args.unit)
    actuals = {}
    for pair in args.actual:
        name, sep, raw = pair.partition("=")
        try:
            value = float(raw) if sep else None
        except ValueError:
            value = None
        if not name or value is None:
            emit("claims.usage", f"--actual expects NAME=VALUE, got {pair!r}")
            return 1
        actuals[name] = value

    failed = False
    for memo in args.memos:
        try:
            text = Path(memo).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            emit("claims.unreadable", f"cannot read report: {exc}", path=memo)
            failed = True
            continue
        claims, problems = parse_memo(text)
        for rule, lineno, message in problems:
            emit(rule, message, path=memo, line=lineno)
            failed = True
        if not claims and not problems:
            emit("claims.no_claims", "no claim lines: a report with no claims cannot earn the gate",
                 path=memo)
            failed = True

        for name in args.require:
            if name not in claims:
                emit("claims.missing_required", f"required claim '{name}' is missing: a deleted "
                     f"or typo'd claim line must fail the gate, not skip verification",
                     path=memo)
                failed = True
        for name, actual in actuals.items():
            if name not in claims:
                emit("claims.missing_required", f"claim '{name}' has a recomputed --actual but "
                     f"the report does not state it", path=memo)
                failed = True

        for name, entries in claims.items():
            if len(entries) > 1:
                emit("claims.duplicate", f"claim '{name}' is stated {len(entries)} times",
                     path=memo, line=entries[1][2])
                failed = True
            for value, source, lineno in entries:
                if source is None:
                    emit("claims.no_source", f"claim '{name}' cites no source "
                         f"('| source: <path>')", path=memo, line=lineno)
                    failed = True
                    continue
                cited = unit / source
                if not cited.is_file():
                    emit("claims.source_missing", f"claim '{name}' cites '{source}', "
                         f"which is not a file under the unit", path=memo, line=lineno)
                    failed = True
                    continue
                if args.mode == "values":
                    source_text = cited.read_text(encoding="utf-8", errors="replace")
                    if not any(abs(value - found) <= args.tolerance
                               for found in numbers_in(source_text)):
                        emit("claims.not_in_source", f"claim '{name}' = {value:g} does not "
                             f"appear in '{source}'", path=memo, line=lineno)
                        failed = True

        for name, actual in actuals.items():
            if name in claims:
                value = claims[name][0][0]
                if abs(value - actual) > args.tolerance:
                    emit("claims.refuted", f"memo claims {name} = {value:g}, recomputed "
                         f"actual is {actual:g}", path=memo)
                    failed = True

    if not failed:
        print(f"verify_claims: OK, {len(args.memos)} report(s), mode {args.mode}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
