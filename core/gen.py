"""gen.py: derived doc views, regenerated between <!-- fh:gen --> markers.

Canonical data (gates, commands) lives ONLY in adapter.yaml; docs carry
rendered views inside marker blocks:

    <!-- fh:gen gate-table -->
    ...rendered content...
    <!-- /fh:gen -->

`gatebench gen` rewrites the blocks; harness_lint's dup-canon check re-renders
and compares, so a hand-edited or stale view fails the build instead of
rotting. This module is deliberately dependency-free (only needs a
Config-shaped object), so gatebench.py can load it standalone from any core
checkout.
"""
from __future__ import annotations

import re
from pathlib import Path

BLOCK_RE = re.compile(
    r"(<!-- fh:gen ([\w-]+) -->)\n?(.*?)(<!-- /fh:gen -->)", re.S)
OPEN_RE = re.compile(r"<!-- fh:gen [\w-]+ -->")
CLOSE_MARK = "<!-- /fh:gen -->"
GEN_DOC_GLOBS = ["*.md", "docs/**/*.md", ".claude/**/*.md"]


class GenError(Exception):
    """Malformed fh:gen marker structure: regenerating would destroy content
    (an unclosed opener pairs with a LATER block's closer and the substitution
    swallows everything between, hand-written sections included)."""


def check_markers(text: str) -> str | None:
    """A problem description when the marker structure is unsafe, else None."""
    opens, closes = len(OPEN_RE.findall(text)), text.count(CLOSE_MARK)
    if opens != closes:
        return (f"unbalanced fh:gen markers ({opens} opener(s), "
                f"{closes} closer(s))")
    for m in BLOCK_RE.finditer(text):
        if "<!-- fh:gen " in m.group(3):
            return (f"fh:gen block {m.group(2)!r} swallows another opener: "
                    f"a closing marker is missing above it")
    return None


def render_gate_table(cfg) -> str:
    rows = ["| # | Gate | Hashed | Command |", "|---|---|---|---|"]
    for i, g in enumerate(cfg.gates, 1):
        hashed = "yes" if g in cfg.hashed_gates else ""
        cmd = str(cfg.gate_commands.get(g, ""))
        rows.append(f"| {i} | `{g}` | {hashed} | `{cmd}` |")
    rows.append("| n/a | `escalated` | | terminal: stuck + written escalation note |")
    return "\n".join(rows)


RENDERERS = {"gate-table": render_gate_table}


def render_block(name: str, cfg) -> str | None:
    """Rendered body for a named block, or None for an unknown name."""
    renderer = RENDERERS.get(name)
    return renderer(cfg) if renderer else None


def regenerate_text(text: str, cfg) -> tuple[str, list[str]]:
    """Rewrite every known gen block; returns (new_text, block names touched).
    Unknown block names are left untouched (dup-canon flags them).
    Raises GenError instead of rewriting a malformed marker structure."""
    problem = check_markers(text)
    if problem:
        raise GenError(problem)
    touched: list[str] = []

    def _sub(m: re.Match) -> str:
        body = render_block(m.group(2), cfg)
        if body is None:
            return m.group(0)
        touched.append(m.group(2))
        return f"{m.group(1)}\n{body}\n{m.group(4)}"

    return BLOCK_RE.sub(_sub, text), touched


def iter_gen_docs(root: Path) -> list[Path]:
    """Docs that may carry gen blocks (root md + docs/ + .claude/)."""
    seen, out = set(), []
    for pat in GEN_DOC_GLOBS:
        for f in sorted(Path(root).glob(pat)):
            if f.is_file() and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def regenerate_project(root: Path, cfg) -> list[Path]:
    """Regenerate every gen block under root; returns the files changed.
    Pre-scans ALL docs and raises GenError before writing ANY file if one is
    malformed, a partial regeneration is worse than none."""
    docs = []
    problems = []
    for doc in iter_gen_docs(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        if "fh:gen" not in text:
            continue
        problem = check_markers(text)
        if problem:
            problems.append(f"{doc.relative_to(root)}: {problem}")
        docs.append((doc, text))
    if problems:
        raise GenError("; ".join(problems) + " (no files were changed)")
    changed = []
    for doc, text in docs:
        new_text, _ = regenerate_text(text, cfg)
        if new_text != text:
            doc.write_text(new_text, encoding="utf-8")
            changed.append(doc)
    return changed


def main(argv=None) -> int:
    """Project-local regeneration: `python3 harness/core/gen.py` (F1). A
    stamped project has no gatebench binary, so the remedy dup-canon names
    must live here. Imports stay inside main() to keep the module itself
    dependency-free."""
    import argparse
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core import config, paths
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()
    try:
        cfg = config.load(root)
        changed = regenerate_project(root, cfg)
    except (config.ConfigError, GenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for f in changed:
        print(f"  regenerated: {f.relative_to(root)}")
    print(f"gen: {len(changed)} file(s) regenerated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
