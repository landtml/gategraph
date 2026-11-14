"""Shared reader for the data pack's checks: the declared spec and one CSV table.

A check runs with the project root as its working directory and takes the unit
directory (repo-relative) as its one argument. It prints one JSON finding per line
on standard output and exits 0 (no fatal finding) or 1 (a fatal finding), which is
the check exit-code contract of kernel/checks.py.

Standard library only: csv, datetime, decimal, json, re, sys.
"""
import csv
import datetime
import decimal
import json
import re
import sys
from pathlib import Path

TYPES = ("integer", "decimal", "date", "text")
SPEC = "spec.json"
INTEGER = re.compile(r"[+-]?\d+")
DECIMAL = re.compile(r"[+-]?\d+(\.\d+)?")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class Stop(Exception):
    """The unit cannot be read further; ``findings`` are the result."""

    def __init__(self, findings):
        super().__init__("unit unreadable")
        self.findings = findings


class Table:
    """A CSV table: header names, and rows as (physical line, values) with None for empty."""

    def __init__(self, path, columns, rows):
        self.path = path
        self.columns = columns
        self.rows = rows


def fatal(rule, message, path=None, line=None):
    return {"rule": rule, "severity": "fatal", "path": path, "line": line, "message": message}


def read_spec(unit):
    path = unit / SPEC
    where = str(path)
    if not path.is_file():
        raise Stop([fatal("spec.missing", f"{where} is missing", where)])
    try:
        spec = json.loads(path.read_text(encoding="utf-8"), parse_float=decimal.Decimal)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise Stop([fatal("spec.invalid", f"{where} is not readable JSON: {exc}", where)])
    problems = _spec_problems(spec)
    if problems:
        raise Stop([fatal("spec.invalid", f"{where}: {p}", where) for p in problems])
    return spec


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _spec_problems(spec):
    if not isinstance(spec, dict):
        return ["the root must be an object"]
    problems = []
    source = spec.get("source")
    if not (isinstance(source, dict) and source.get("kind") == "csv"
            and isinstance(source.get("path"), str)):
        problems.append("source must be {kind: csv, path: <file in the unit>}")
    else:
        path = source["path"]
        if path.startswith("/") or ".." in path.split("/"):
            problems.append("source.path must stay inside the unit")
    columns = spec.get("columns")
    if not isinstance(columns, list) or not columns:
        problems.append("columns must be a non-empty list")
    else:
        for index, column in enumerate(columns):
            if not (isinstance(column, dict) and isinstance(column.get("name"), str)
                    and column.get("type") in TYPES):
                problems.append(f"columns[{index}] needs a name and a type in {list(TYPES)}")
    rows = spec.get("rows")
    if not (isinstance(rows, dict) and _is_int(rows.get("min")) and _is_int(rows.get("max"))
            and 0 <= rows["min"] <= rows["max"]):
        problems.append("rows must be {min: <int>, max: <int>} with 0 <= min <= max")
    rate = spec.get("max_null_rate")
    if not (isinstance(rate, (int, decimal.Decimal)) and not isinstance(rate, bool)
            and 0 <= rate <= 1):
        problems.append("max_null_rate must be a number from 0 to 1")
    totals = spec.get("totals")
    if not isinstance(totals, list):
        problems.append("totals must be a list")
    else:
        for index, total in enumerate(totals):
            if not (isinstance(total, dict) and isinstance(total.get("column"), str)
                    and isinstance(total.get("declared"), str)
                    and DECIMAL.fullmatch(total["declared"])):
                problems.append(f"totals[{index}] needs a column and a declared decimal string")
    return problems


def read_table(unit, spec):
    path = unit / spec["source"]["path"]
    where = str(path)
    if not path.is_file():
        raise Stop([fatal("source.missing", f"{where} is missing", where)])
    try:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration:
                raise Stop([fatal("source.empty", f"{where} has no header line", where, 1)])
            rows = []
            for row in reader:
                line = reader.line_num
                if not row:
                    continue
                if len(row) != len(header):
                    raise Stop([fatal("source.ragged-row",
                                      f"{len(row)} fields, the header has {len(header)}",
                                      where, line)])
                rows.append((line, [value if value != "" else None for value in row]))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise Stop([fatal("source.unreadable", f"{where}: {exc}", where)])
    return Table(where, header, rows)


def conforms(kind, value):
    if kind == "text":
        return True
    if kind == "integer":
        return INTEGER.fullmatch(value) is not None
    if kind == "decimal":
        return DECIMAL.fullmatch(value) is not None
    if kind == "date":
        if DATE.fullmatch(value) is None:
            return False
        try:
            datetime.date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return False


def run(check):
    """Entry point of every check: argv[1] is the unit; returns the exit code."""
    if len(sys.argv) != 2:
        findings = [fatal("check.usage", "usage: <check> <unit-dir>")]
    else:
        try:
            unit = Path(sys.argv[1])
            spec = read_spec(unit)
            table = read_table(unit, spec)
            findings = list(check(spec, table))
        except Stop as stop:
            findings = stop.findings
    for finding in findings:
        print(json.dumps(finding, sort_keys=True))
    return 1 if any(f["severity"] == "fatal" for f in findings) else 0
