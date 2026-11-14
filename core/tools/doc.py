"""Document tools: ``extract_text`` and ``render_document`` (plan section 1.10, brief A3).

Both are thin wrappers over external programs, given as argument vectors with
``{path}``, ``{source}`` or ``{output}`` placeholders. No shell is used. A
missing program gives a plain result that says so, not an exception. The
default extractor is the one the kernel uses for PDFs (``pdftotext``).

``render_document`` may write only under the unit directory or the scratch
directory. ``extract_text`` reads only, and it declares ``exec`` because it runs
an external program.

Standard library and ``core.kernel`` only.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Sequence

from core.kernel.extract import DEFAULT_PDF_COMMAND
from core.kernel.globs import glob_match, normalize_path
from core.kernel.types import Action, ActionKind
from core.tools.spec import ToolContext, ToolResult, ToolSpec

DEFAULT_MAX_CHARS = 20_000
EXTRACT_TIMEOUT_S = 60.0
RENDER_TIMEOUT_S = 120.0


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


def _is_blind(rel: str, blind: tuple[str, ...]) -> bool:
    normal = normalize_path(rel)
    return any(glob_match(pattern, normal) for pattern in blind)


def _refused(reason: str) -> ToolResult:
    return ToolResult(content=f"refused: {reason}")


def _fill(template: Sequence[str], values: dict[str, str]) -> list[str]:
    argv = []
    for part in template:
        for key, value in values.items():
            part = part.replace("{" + key + "}", value)
        argv.append(part)
    return argv


def make_extract_text(*, command: Sequence[str] = DEFAULT_PDF_COMMAND,
                      max_chars: int = DEFAULT_MAX_CHARS) -> ToolSpec:
    """Build ``extract_text``. ``command`` is an argv template; its stdout is the text."""
    if not command:
        raise ValueError("extract_text needs an extractor command")
    template = tuple(command)

    def to_action(args: dict) -> Action:
        path = args.get("path")
        if not isinstance(path, str):
            raise ValueError("extract_text needs a string 'path'")
        return Action(kind=ActionKind.READ, tool="extract_text", path=path)

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        rel = args.get("path")
        try:
            full = _workspace_file(ctx.root, rel)
        except ValueError as exc:
            return _refused(str(exc))
        if _is_blind(rel, ctx.blind):
            return _refused(f"path {rel!r} is blind and may not be read")
        if not full.is_file():
            return ToolResult(content=f"not found: {rel}")
        argv = _fill(template, {"path": str(full)})
        if shutil.which(argv[0]) is None:
            return ToolResult(content=(
                f"extractor not installed: {argv[0]!r} is not on PATH, so {rel} was not read. "
                "Install the extractor to get text from this file type."))
        try:
            proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=EXTRACT_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult(content=f"extractor failed to run: {exc}")
        if proc.returncode != 0:
            first = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
            detail = first[0] if first else f"exit status {proc.returncode}"
            return ToolResult(content=f"extractor failed on {rel}: {detail}")
        text = proc.stdout.decode("utf-8", errors="replace")
        if len(text) > max_chars:
            return ToolResult(content=text[:max_chars] + f"\n[truncated at {max_chars} characters]")
        return ToolResult(content=text)

    return ToolSpec(
        name="extract_text",
        description="Extract plain text from a workspace file with the configured extractor.",
        args_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        effects=frozenset({"read", "exec"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )


def make_render_document(*, command: Sequence[str]) -> ToolSpec:
    """Build ``render_document``. ``command`` must name ``{source}`` and ``{output}``."""
    template = tuple(command)
    if not template or not any("{output}" in part for part in template):
        raise ValueError("render_document needs a command template that writes to {output}")

    def to_action(args: dict) -> Action:
        output = args.get("output")
        if not isinstance(output, str):
            raise ValueError("render_document needs a string 'output'")
        return Action(kind=ActionKind.WRITE, tool="render_document", path=output)

    def run(args: dict, ctx: ToolContext) -> ToolResult:
        src_rel = args.get("source")
        out_rel = args.get("output")
        try:
            src = _workspace_file(ctx.root, src_rel)
            out = _workspace_file(ctx.root, out_rel)
        except ValueError as exc:
            return _refused(str(exc))
        if _is_blind(src_rel, ctx.blind):
            return _refused(f"path {src_rel!r} is blind and may not be read")
        writable_roots = (ctx.unit_dir.resolve(), ctx.scratch_dir.resolve())
        if not any(out.is_relative_to(root) for root in writable_roots):
            return _refused("output must be inside the unit directory or the scratch directory")
        if not src.is_file():
            return ToolResult(content=f"not found: {src_rel}")
        argv = _fill(template, {"source": str(src), "output": str(out)})
        if shutil.which(argv[0]) is None:
            return ToolResult(content=(
                f"renderer not installed: {argv[0]!r} is not on PATH, so {out_rel} was not written."))
        out.parent.mkdir(parents=True, exist_ok=True)  # already inside an allowed root
        try:
            proc = subprocess.run(argv, cwd=str(ctx.unit_dir), stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=RENDER_TIMEOUT_S, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResult(content=f"renderer failed to run: {exc}")
        if proc.returncode != 0:
            first = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()[:1]
            detail = first[0] if first else f"exit status {proc.returncode}"
            return ToolResult(content=f"renderer failed on {src_rel}: {detail}")
        if not out.is_file():
            return ToolResult(content=f"renderer exited 0 but wrote no file at {out_rel}")
        normal = out.relative_to(ctx.root.resolve()).as_posix()
        return ToolResult(content=f"rendered {src_rel} to {normal}", touched=(normal,))

    return ToolSpec(
        name="render_document",
        description="Render a workspace document to an output inside the unit or scratch directory.",
        args_schema={
            "type": "object",
            "properties": {"source": {"type": "string"}, "output": {"type": "string"}},
            "required": ["source", "output"],
            "additionalProperties": False,
        },
        effects=frozenset({"read", "write", "exec"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )
