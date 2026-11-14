"""Data tool: ``query_table`` (plan section 1.10, brief A3).

Read-only SQL over a SQLite file inside the workspace. The statement must be a
single ``SELECT`` or ``WITH`` query. The connection opens the file read-only and
turns on ``PRAGMA query_only``, so a write is refused even when the statement
check misses it. The database path must stay inside the workspace and must not
match a blind pattern. Engines other than SQLite are not implemented here.

Standard library and ``core.kernel`` only.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import time
import urllib.parse
from typing import Optional

from core.kernel.globs import glob_match, normalize_path
from core.kernel.types import Action, ActionKind
from core.tools.spec import ToolContext, ToolResult, ToolSpec

DEFAULT_MAX_ROWS = 200
QUERY_TIMEOUT_S = 10.0
READ_LEADERS = frozenset({"select", "with"})
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"--[^\n]*")


def _strip_leading_comments(sql: str) -> str:
    text = sql
    while True:
        text = text.lstrip()
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline < 0 else text[newline + 1:]
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            if end < 0:
                return ""
            text = text[end + 2:]
        else:
            return text


def statement_refusal(sql: str) -> Optional[str]:
    """Return why ``sql`` is not a single read-only statement, or None when it is one."""
    if not isinstance(sql, str):
        return "the SQL must be a string"
    trimmed = sql.strip().rstrip(";").strip()
    if not trimmed:
        return "the SQL is empty"
    # A semicolon anywhere but the end is refused, even inside a string literal.
    if ";" in trimmed:
        return "only one statement may be run"
    body = _strip_leading_comments(sql).lstrip()
    match = re.match(r"[A-Za-z]+", body)
    word = match.group(0).lower() if match else ""
    if word not in READ_LEADERS:
        shown = word.upper() if word else "nothing"
        return f"only SELECT and WITH queries are allowed; the statement starts with {shown}"
    return None


def _is_blind(rel: str, blind: tuple[str, ...]) -> bool:
    normal = normalize_path(rel)
    return any(glob_match(pattern, normal) for pattern in blind)


def _workspace_file(root: pathlib.Path, rel: str) -> pathlib.Path:
    """Resolve a workspace-relative path. Refuse absolute paths and escapes."""
    if not isinstance(rel, str) or rel == "":
        raise ValueError("path must be a non-empty string")
    posix = pathlib.PurePosixPath(rel)
    windows = pathlib.PureWindowsPath(rel)
    if posix.is_absolute() or windows.drive or windows.root or ".." in windows.parts:
        raise ValueError(f"path must stay inside the workspace: {rel!r}")
    base = root.resolve()
    full = (root / rel).resolve()
    if not full.is_relative_to(base):
        raise ValueError(f"path must stay inside the workspace: {rel!r}")
    return full


def _refused(reason: str) -> ToolResult:
    return ToolResult(content=f"refused: {reason}")


def _cell(value) -> str:
    return "NULL" if value is None else str(value)


def make_query_table(*, max_rows: int = DEFAULT_MAX_ROWS,
                     timeout_s: float = QUERY_TIMEOUT_S) -> ToolSpec:
    """Build the read-only ``query_table`` tool."""
    if max_rows < 1:
        raise ValueError("max_rows must be at least 1")

    def to_action(args: dict) -> Action:
        path = args.get("path")
        if not isinstance(path, str):
            raise ValueError("query_table needs a string 'path'")
        return Action(kind=ActionKind.READ, tool="query_table", path=path)

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        sql = args.get("sql")
        try:
            full = _workspace_file(ctx.root, rel)
        except ValueError as exc:
            return _refused(str(exc))
        if _is_blind(rel, ctx.blind):
            return _refused(f"path {rel!r} is blind and may not be read")
        reason = statement_refusal(sql)
        if reason is not None:
            return _refused(reason)
        if not full.is_file():
            return ToolResult(content=f"not found: {rel}")

        uri = "file:" + urllib.parse.quote(str(full)) + "?mode=ro"
        deadline = time.monotonic() + timeout_s
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=timeout_s)
        except sqlite3.Error as exc:
            return ToolResult(content=f"query failed: cannot open {rel}: {exc}")
        try:
            conn.execute("PRAGMA query_only = ON")
            conn.set_progress_handler(
                lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            rows = cursor.fetchmany(max_rows + 1)
        except sqlite3.OperationalError as exc:
            if "readonly" in str(exc).lower() or "read-only" in str(exc).lower():
                return _refused("the statement tries to write, and the database is read-only")
            return ToolResult(content=f"query failed: {exc}")
        except sqlite3.Error as exc:
            return ToolResult(content=f"query failed: {exc}")
        finally:
            conn.close()

        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        lines = []
        if columns:
            lines.append("\t".join(columns))
        lines.extend("\t".join(_cell(v) for v in row) for row in rows)
        footer = f"({len(rows)} rows"
        footer += f"; truncated at {max_rows} rows)" if truncated else ")"
        lines.append(footer)
        return ToolResult(content="\n".join(lines))

    return ToolSpec(
        name="query_table",
        description=("Run one read-only SELECT or WITH query against a SQLite file in the "
                     "workspace. Writes are refused."),
        args_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "sql": {"type": "string"}},
            "required": ["path", "sql"],
            "additionalProperties": False,
        },
        effects=frozenset({"read"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )
