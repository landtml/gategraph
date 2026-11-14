"""Generated doc blocks: derived views rendered between marker comments.

Canonical data (gates, checks) lives only in the adapter. A doc carries a
rendered view inside a block: an opener of the form ``<!-- MARKER NAME -->``,
the rendered content, and ``<!-- /MARKER -->``, where MARKER is brand.GEN_MARKER.
brand.GEN_MARKER is the only spelling written. brand.LEGACY_GEN_MARKERS are
read, so an old block is regenerated into the current spelling (bug B41).
Regeneration refuses a malformed marker structure before it writes any file.

Standard library only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from core import brand

MARKERS = (brand.GEN_MARKER, *brand.LEGACY_GEN_MARKERS)
_MARKER_ALT = "|".join(re.escape(m) for m in MARKERS)
BLOCK_RE = re.compile(
    rf"(<!-- ({_MARKER_ALT}) ([\w-]+) -->)\n?(.*?)(<!-- /\2 -->)", re.S)
OPEN_RE = re.compile(rf"<!-- (?:{_MARKER_ALT}) [\w-]+ -->")
GEN_DOC_GLOBS = ["*.md", "docs/**/*.md", ".claude/**/*.md"]


class GenError(Exception):
    """Malformed marker structure: regenerating would destroy content (an
    unclosed opener pairs with a LATER block's closer and the substitution
    swallows everything between, hand-written sections included)."""


def check_markers(text: str) -> str | None:
    """A problem description when the marker structure is unsafe, else None."""
    opens = len(OPEN_RE.findall(text))
    closes = sum(text.count(f"<!-- /{m} -->") for m in MARKERS)
    if opens != closes:
        return f"unbalanced gen markers ({opens} opener(s), {closes} closer(s))"
    for m in BLOCK_RE.finditer(text):
        if OPEN_RE.search(m.group(4)):
            return (f"gen block {m.group(3)!r} swallows another opener: "
                    f"a closing marker is missing above it")
    return None


def render_gate_table(cfg) -> str:
    rows = ["| # | Gate | Needs | Checks |", "|---|---|---|---|"]
    for i, gate in enumerate(cfg.gates, 1):
        needs = ", ".join(gate.needs)
        checks = ", ".join(gate.checks)
        rows.append(f"| {i} | `{gate.id}` | {needs} | {checks} |")
    rows.append("| n/a | `escalated` | | terminal: stuck + written escalation note |")
    return "\n".join(rows)


RENDERERS = {"gate-table": render_gate_table}


def render_block(name: str, cfg) -> str | None:
    """Rendered body for a named block, or None for an unknown name."""
    renderer = RENDERERS.get(name)
    return renderer(cfg) if renderer else None


def regenerate_text(text: str, cfg) -> tuple[str, list[str]]:
    """Rewrite every known block into the current spelling; returns
    (new_text, block names touched). Unknown names are left untouched.
    Raises GenError instead of rewriting a malformed marker structure."""
    problem = check_markers(text)
    if problem:
        raise GenError(problem)
    touched: list[str] = []

    def _sub(m: re.Match) -> str:
        name = m.group(3)
        body = render_block(name, cfg)
        if body is None:
            return m.group(0)
        touched.append(name)
        return (f"<!-- {brand.GEN_MARKER} {name} -->\n{body}\n"
                f"<!-- /{brand.GEN_MARKER} -->")

    return BLOCK_RE.sub(_sub, text), touched


def iter_gen_docs(root: Path) -> list[Path]:
    """Docs that may carry gen blocks (root md, docs/ and .claude/)."""
    seen, out = set(), []
    for pat in GEN_DOC_GLOBS:
        for f in sorted(Path(root).glob(pat)):
            if f.is_file() and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def regenerate(root: Path, cfg) -> list[Path]:
    """Regenerate every gen block under root; returns the files changed.
    Pre-scans every doc and raises GenError before writing any file: a partial
    regeneration is worse than none."""
    root = Path(root)
    docs, problems = [], []
    for doc in iter_gen_docs(root):
        text = doc.read_text(encoding="utf-8", errors="replace")
        if not any(m in text for m in MARKERS):
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
    import argparse
    from core.kernel import config, paths
    p = argparse.ArgumentParser(description="Regenerate generated doc blocks.")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_root()
    try:
        cfg = config.load(root)
        changed = regenerate(root, cfg)
    except (config.ConfigError, GenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for f in changed:
        print(f"  regenerated: {f.relative_to(root)}")
    print(f"gen: {len(changed)} file(s) regenerated.")
    return 0


if __name__ == "__main__":  # run from the repository root: python3 -m core.kernel.gen
    raise SystemExit(main())
