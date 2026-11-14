"""File tools for the native actor (plan section 1.10, brief A1).

Tools: read_file, list_dir, glob, grep (read); write_file, edit_file (write).

Path rules, applied to every path before anything is read or written:

- A relative path is taken from the workspace root. An absolute path is allowed
  only if it resolves inside the root.
- The path is resolved with symlinks followed, and the resolved path is what is
  checked. A symlink that points outside the workspace is refused.
- A path matching a ``blind`` pattern (or naming a directory a blind pattern lies
  under) is refused for read and write, and never listed, globbed or searched.
- A write must resolve inside the unit directory or the scratch directory, and
  its workspace-relative path must match a ``writable`` pattern.

Refusals raise ``ToolRefused`` and happen before the write. Ordinary usage
errors (no such file, text not found) come back as a ``ToolResult`` whose content
says so, and touch nothing. Patterns use the kernel glob dialect (core.kernel.globs).

Standard library, core.kernel and core.tools only.
"""
from __future__ import annotations

import os
import pathlib
import re

from core.kernel.globs import glob_match
from core.kernel.paths import atomic_write
from core.kernel.types import Action, ActionKind
from core.tools.registry import ToolRefused
from core.tools.spec import ToolContext, ToolResult, ToolSpec

MAX_READ_CHARS = 200_000
MAX_SEARCH_FILE_BYTES = 5_000_000
MAX_LIST_ENTRIES = 500
MAX_GREP_HITS = 200


# --------------------------------------------------------------------------
# Path checks
# --------------------------------------------------------------------------

def _root(ctx: ToolContext) -> pathlib.Path:
    return pathlib.Path(ctx.root).resolve()


def _within(path: pathlib.Path, directory: pathlib.Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _rel(path: pathlib.Path, root: pathlib.Path) -> str:
    rel = path.relative_to(root).as_posix()
    return "" if rel == "." else rel


def _is_blind(ctx: ToolContext, rel: str) -> bool:
    # The second probe catches a directory that a blind pattern lies under,
    # because "dir/**" needs at least one segment below "dir".
    return any(glob_match(pattern, rel) or glob_match(pattern, rel + "/.probe")
               for pattern in ctx.blind)


def _locate(ctx: ToolContext, raw) -> tuple[pathlib.Path, str]:
    """Resolve a model-supplied path. Returns (resolved path, workspace-relative path)."""
    if not isinstance(raw, str) or raw.strip() == "":
        raise ToolRefused("path must be a non-empty string")
    root = _root(ctx)
    variant = pathlib.Path(raw)
    if not variant.is_absolute():
        variant = root / variant
    try:
        resolved = variant.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ToolRefused(f"path {raw!r} cannot be resolved: {exc}") from exc
    if not _within(resolved, root):
        raise ToolRefused(f"path {raw!r} is outside the workspace")
    return resolved, _rel(resolved, root)


def _check_read(ctx: ToolContext, raw) -> tuple[pathlib.Path, str]:
    resolved, rel = _locate(ctx, raw)
    if _is_blind(ctx, rel):
        raise ToolRefused(f"read refused: {rel} is a blind path")
    return resolved, rel


def _check_write(ctx: ToolContext, raw) -> tuple[pathlib.Path, str]:
    resolved, rel = _locate(ctx, raw)
    homes = (pathlib.Path(ctx.unit_dir).resolve(), pathlib.Path(ctx.scratch_dir).resolve())
    if not any(_within(resolved, home) for home in homes):
        raise ToolRefused(f"write refused: {rel} is outside the unit and scratch directories")
    if _is_blind(ctx, rel):
        raise ToolRefused(f"write refused: {rel} is a blind path")
    if not any(glob_match(pattern, rel) for pattern in ctx.writable):
        raise ToolRefused(f"write refused: {rel} is not a writable path")
    return resolved, rel


def _visible(ctx: ToolContext, path: pathlib.Path) -> bool:
    """True if a path (after symlink resolution) is inside the workspace and not blind."""
    root = _root(ctx)
    resolved = path.resolve()
    if not _within(resolved, root):
        return False
    return not _is_blind(ctx, _rel(resolved, root))


def _walk_visible(ctx: ToolContext, base: pathlib.Path):
    """Yield visible regular files under base, in sorted order."""
    for dirpath, dirnames, filenames in os.walk(base):
        current = pathlib.Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if _visible(ctx, current / d))
        for name in sorted(filenames):
            full = current / name
            if full.is_file() and _visible(ctx, full):
                yield full


# --------------------------------------------------------------------------
# Read tools
# --------------------------------------------------------------------------

def read_file(args: dict, ctx: ToolContext) -> ToolResult:
    resolved, rel = _check_read(ctx, args["path"])
    if not resolved.is_file():
        return ToolResult(content=f"error: {rel} is not a file")
    text = resolved.read_text(encoding="utf-8", errors="replace")
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + f"\n[truncated at {MAX_READ_CHARS} characters]"
    return ToolResult(content=text)


def list_dir(args: dict, ctx: ToolContext) -> ToolResult:
    resolved, rel = _check_read(ctx, args.get("path", "."))
    if not resolved.is_dir():
        return ToolResult(content=f"error: {rel or '.'} is not a directory")
    entries = []
    for child in sorted(resolved.iterdir(), key=lambda p: p.name):
        if not _visible(ctx, child):
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
        if len(entries) >= MAX_LIST_ENTRIES:
            entries.append(f"[stopped at {MAX_LIST_ENTRIES} entries]")
            break
    return ToolResult(content="\n".join(entries))


def glob(args: dict, ctx: ToolContext) -> ToolResult:
    """Files under ``path`` whose path relative to ``path`` matches ``pattern``."""
    pattern = args["pattern"]
    if not isinstance(pattern, str) or pattern == "":
        return ToolResult(content="error: pattern must be a non-empty string")
    resolved, rel = _check_read(ctx, args.get("path", "."))
    if not resolved.is_dir():
        return ToolResult(content=f"error: {rel or '.'} is not a directory")
    root = _root(ctx)
    hits = []
    for full in _walk_visible(ctx, resolved):
        if glob_match(pattern, full.relative_to(resolved).as_posix()):
            hits.append(_rel(full, root))
            if len(hits) >= MAX_LIST_ENTRIES:
                hits.append(f"[stopped at {MAX_LIST_ENTRIES} entries]")
                break
    return ToolResult(content="\n".join(hits))


def grep(args: dict, ctx: ToolContext) -> ToolResult:
    """Lines matching a regular expression, as ``path:line:text``. Binary files are skipped."""
    try:
        regex = re.compile(args["pattern"])
    except (re.error, TypeError) as exc:
        return ToolResult(content=f"error: bad pattern: {exc}")
    resolved, rel = _check_read(ctx, args.get("path", "."))
    if not resolved.is_dir():
        return ToolResult(content=f"error: {rel or '.'} is not a directory")
    root = _root(ctx)
    hits = []
    for full in _walk_visible(ctx, resolved):
        if full.stat().st_size > MAX_SEARCH_FILE_BYTES:
            continue
        data = full.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{_rel(full, root)}:{number}:{line}")
                if len(hits) >= MAX_GREP_HITS:
                    hits.append(f"[stopped at {MAX_GREP_HITS} hits]")
                    return ToolResult(content="\n".join(hits))
    return ToolResult(content="\n".join(hits))


# --------------------------------------------------------------------------
# Write tools
# --------------------------------------------------------------------------

def write_file(args: dict, ctx: ToolContext) -> ToolResult:
    content = args["content"]
    if not isinstance(content, str):
        return ToolResult(content="error: content must be a string")
    resolved, rel = _check_write(ctx, args["path"])
    if resolved.is_dir():
        return ToolResult(content=f"error: {rel} is a directory")
    atomic_write(resolved, content)
    return ToolResult(content=f"wrote {len(content)} characters to {rel}", touched=(rel,))


def edit_file(args: dict, ctx: ToolContext) -> ToolResult:
    old, new = args["old"], args["new"]
    replace_all = bool(args.get("replace_all", False))
    if not isinstance(old, str) or old == "" or not isinstance(new, str):
        return ToolResult(content="error: old must be a non-empty string and new a string")
    resolved, rel = _check_write(ctx, args["path"])
    if not resolved.is_file():
        return ToolResult(content=f"error: {rel} is not a file")
    text = resolved.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return ToolResult(content=f"edit failed: the text was not found in {rel}")
    if count > 1 and not replace_all:
        return ToolResult(content=f"edit failed: the text occurs {count} times in {rel}; "
                                  "pass replace_all to change every occurrence")
    if replace_all:
        updated, changed = text.replace(old, new), count
    else:
        updated, changed = text.replace(old, new, 1), 1
    atomic_write(resolved, updated)
    return ToolResult(content=f"edited {rel} ({changed} replacement(s))", touched=(rel,))


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------

_PATH = {"type": "string", "description": "Workspace-relative path."}


def _spec(name, description, properties, required, kind, run, *, idempotent,
          text_arg=None):
    """``text_arg`` names the argument whose text the tool writes (``content`` for
    write_file, ``new`` for edit_file). The guard scans it before the write, the same
    text the legacy pre-write hook scans for an Edit (its ``new_string``)."""
    effects = frozenset({"read"} if kind is ActionKind.READ else {"write"})

    def to_action(args: dict) -> Action:
        content = None
        if text_arg is not None:
            content = args.get(text_arg)
            if not isinstance(content, str):
                raise ValueError(f"{name}: {text_arg} must be a string")
        return Action(kind=kind, tool=name, path=args.get("path"), content=content)

    return ToolSpec(
        name=name,
        description=description,
        args_schema={"type": "object", "properties": properties,
                     "required": list(required), "additionalProperties": False},
        effects=effects,
        to_action=to_action,
        run=run,
        idempotent=idempotent,
    )


def specs() -> tuple[ToolSpec, ...]:
    return (
        _spec("read_file", "Read a text file.",
              {"path": _PATH}, ["path"], ActionKind.READ, read_file, idempotent=True),
        _spec("list_dir", "List the entries of a directory.",
              {"path": _PATH}, [], ActionKind.READ, list_dir, idempotent=True),
        _spec("glob", "List files under a directory matching a glob (kernel dialect).",
              {"pattern": {"type": "string"}, "path": _PATH}, ["pattern"],
              ActionKind.READ, glob, idempotent=True),
        _spec("grep", "Search files under a directory for a regular expression.",
              {"pattern": {"type": "string"}, "path": _PATH}, ["pattern"],
              ActionKind.READ, grep, idempotent=True),
        _spec("write_file", "Create or replace a file inside the writable paths.",
              {"path": _PATH, "content": {"type": "string"}}, ["path", "content"],
              ActionKind.WRITE, write_file, idempotent=True, text_arg="content"),
        _spec("edit_file", "Replace text in a file inside the writable paths.",
              {"path": _PATH, "old": {"type": "string"}, "new": {"type": "string"},
               "replace_all": {"type": "boolean"}},
              ["path", "old", "new"], ActionKind.EDIT, edit_file, idempotent=False,
              text_arg="new"),
    )
