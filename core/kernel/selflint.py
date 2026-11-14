"""Self-lint: the harness checks its own documents, config and tree.

Tiers, by detection reliability:
  GATING    deterministic, no false positives by design, fails the build (exit 2):
            doc-cmd-drift, orphan-refs, dup-canon, instance-leak,
            committed-state, brand-literal
  WARNING   heuristic, reported, exit 1: stale-state (fix deletes), unit-slug
  ADVISORY  heuristic, a human-reviewed worklist, never affects the exit code:
            honor-rules-audit

Host-specific checks (hook registration in a settings file) are not defined here; the
host module owns them (core.hosts.claude_code.check_registration), and the gating tier
runs them. Template mode: a root with no harness/adapter/adapter.yaml (the template itself) skips the
project-only checks (adapter-complete, hook registration) and says so; the rest run against an
empty configuration. Run by path or as a module. Standard library and PyYAML only.

Usage: python3 -m core.kernel.selflint [--tier gating|warning|advisory|all]
                                       [--machine] [--fix] [--root DIR]
"""
from __future__ import annotations

import os
import sys

# Run by path (python3 core/kernel/selflint.py): the script's own directory holds
# core/kernel/types.py, which would shadow the standard library's `types`. Drop that
# entry and put the repository root in front, before any other import.
if __package__ in (None, ""):
    _here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != _here]
    sys.path.insert(0, os.path.dirname(os.path.dirname(_here)))

import argparse
import re
from fnmatch import fnmatch
from pathlib import Path
from typing import NamedTuple, Sequence

from core import brand
from core.hosts import claude_code
from core.kernel import config, gen, paths, reap

DOC_GLOBS = ("CLAUDE.md", "README.md", "MEMORY.md", "docs/**/*.md", ".claude/**/*.md")
# Historical records (the rebuild plan, its run log, experiment notes) describe
# commands that existed when they were written; they are not live docs.
DOC_EXCLUDE_PREFIXES = ()
HARNESS_PATH_RE = re.compile(r"harness/[\w./-]+?\.(?:py|sh)\b")
MAKE_TARGET_RE = re.compile(r"\bmake ([a-z][\w-]*)")
CODE_SPAN_RE = re.compile(r"`([^`]*)`")
# Bug B13: the gate-name pattern applies only inside code spans and fenced blocks.
GATE_NAME_RE = re.compile(r"\bapprove\s+([a-z][a-z0-9_-]*)")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
HONOR_RE = re.compile(r"\b(never|always|must)\b", re.I)
PATHISH_RE = re.compile(r"\b[\w.-]+/[\w./*-]*")
MAX_LEAK_FILE_BYTES = 1_000_000
UNIT_MANIFEST = "manifest.yaml"
# Bug B37: a unit slug shorter than this is skipped with a warning.
MIN_SLUG_LEN = 3
LEAK_EXCLUDE_PREFIXES = tuple(f"{d}/" for d in (brand.STATE_DIRNAME,
                                                *brand.LEGACY_STATE_DIRNAMES))
DEFAULT_FRAMEWORK_DIRS = ("core", "docs")
# Trees where the product name may not be spelled; core/brand.py is the one home.
BRAND_SCAN_DIRS = ("core/kernel", "core/graph", "core/agent", "core/tools",
                   "core/hosts", "core/compat")
BRAND_EXEMPT = frozenset({"core/brand.py"})
_BRAND_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(x) for x in {brand.NAME, brand.SLUG})
    + r")(?![A-Za-z0-9])", re.I)


class Finding(NamedTuple):
    check: str
    tier: str
    message: str


def _harness_path_exists(root, rel: str) -> bool:
    """A doc path ``harness/core/x`` names the stamped core. At the template root
    (no stamped harness/core) it resolves to the template's own ``core/x``."""
    root = Path(root)
    if (root / rel).is_file():
        return True
    if rel.startswith("harness/core/") and not (root / "harness" / "core").is_dir():
        return (root / rel[len("harness/"):]).is_file()
    return False


def iter_docs(root: Path) -> list[Path]:
    seen, out = set(), []
    for pat in DOC_GLOBS:
        for f in sorted(Path(root).glob(pat)):
            rel = f.relative_to(root).as_posix() if f.is_absolute() else f.as_posix()
            if rel.startswith(DOC_EXCLUDE_PREFIXES):
                continue
            if f.is_file() and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _doc_lines(doc: Path):
    text = doc.read_text(encoding="utf-8", errors="replace")
    for n, line in enumerate(text.splitlines(), 1):
        yield n, line


def _make_targets(root: Path) -> set[str]:
    mk = Path(root) / "Makefile"
    if not mk.is_file():
        return set()
    return {m.group(1) for m in
            re.finditer(r"^([A-Za-z][\w-]*):", mk.read_text(errors="replace"), re.M)}


def unit_slugs(root: Path, unit_dir: str, min_len: int = MIN_SLUG_LEN
               ) -> tuple[list[str], list[str]]:
    """Unit slugs under unit_dir (B37): only directories holding a manifest are
    units. Returns (slugs, warnings); short slugs are skipped with a warning."""
    units = Path(root) / unit_dir
    slugs: list[str] = []
    warnings: list[str] = []
    if not units.is_dir():
        return slugs, warnings
    for d in sorted(units.iterdir()):
        if not d.is_dir() or not (d / UNIT_MANIFEST).is_file():
            continue
        if len(d.name) < min_len:
            warnings.append(f"unit '{d.name}' is shorter than {min_len} characters "
                            f"and is skipped by the leak scan")
            continue
        slugs.append(d.name)
    return slugs, warnings


# ---------------------------------------------------------------- GATING

def check_doc_cmd_drift(root: Path, cfg) -> list[Finding]:
    out = []
    gate_ids = {g.id for g in cfg.gates}
    targets = _make_targets(root)
    for doc in iter_docs(root):
        rel = doc.relative_to(root)
        in_fence = False
        for n, line in _doc_lines(doc):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            for m in HARNESS_PATH_RE.finditer(line):
                if not _harness_path_exists(root, m.group(0)):
                    out.append(Finding("doc-cmd-drift", "gating",
                                       f"{rel}:{n} references missing {m.group(0)}"))
            # Commands count only in code: a fenced block or an inline code span.
            code = [line] if in_fence else CODE_SPAN_RE.findall(line)
            for seg in code:
                for m in MAKE_TARGET_RE.finditer(seg):
                    if m.group(1) not in targets:
                        out.append(Finding("doc-cmd-drift", "gating",
                                           f"{rel}:{n} references missing make "
                                           f"target '{m.group(1)}'"))
                for m in GATE_NAME_RE.finditer(seg):
                    if m.group(1) not in gate_ids:
                        out.append(Finding("doc-cmd-drift", "gating",
                                           f"{rel}:{n} names unknown gate "
                                           f"'{m.group(1)}'"))
    return out


def check_orphan_refs(root: Path, cfg) -> list[Finding]:
    out = []
    docs = iter_docs(root)
    stems = {f.stem for f in docs}
    for doc in docs:
        rel = doc.relative_to(root)
        for n, line in _doc_lines(doc):
            for m in MD_LINK_RE.finditer(line):
                href = m.group(1).split("#")[0].strip()
                if (not href or "://" in href or href.startswith("mailto:")
                        or "{" in href):
                    continue
                if not ((doc.parent / href).exists() or (Path(root) / href).exists()):
                    out.append(Finding("orphan-refs", "gating",
                                       f"{rel}:{n} broken link -> {href}"))
            for m in WIKILINK_RE.finditer(line):
                if m.group(1).strip() not in stems:
                    out.append(Finding("orphan-refs", "gating",
                                       f"{rel}:{n} unresolved wikilink "
                                       f"[[{m.group(1).strip()}]]"))
    for check in cfg.checks:
        for m in HARNESS_PATH_RE.finditer(check.command):
            if not (Path(root) / m.group(0)).is_file():
                out.append(Finding("orphan-refs", "gating",
                                   f"check '{check.id}' command references "
                                   f"missing {m.group(0)}"))
    return out


def check_dup_canon(root: Path, cfg) -> list[Finding]:
    out = []
    command_texts = [(c.id, c.command) for c in cfg.checks if c.command]
    for doc in gen.iter_gen_docs(Path(root)):
        if doc.name == "HANDOFF.md":
            continue  # session state, not canon
        rel = doc.relative_to(root)
        text = doc.read_text(encoding="utf-8", errors="replace")
        problem = gen.check_markers(text)
        if problem:
            out.append(Finding("dup-canon", "gating",
                               f"{rel}: {problem}, regeneration refuses this file "
                               f"until the markers are repaired"))
            continue
        for m in gen.BLOCK_RE.finditer(text):
            name, body = m.group(3), m.group(4).strip("\n")
            rendered = gen.render_block(name, cfg)
            if rendered is None:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: unknown gen block '{name}'"))
            elif body != rendered:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: gen block '{name}' does not match "
                                   f"regeneration: run `python3 -m core.kernel.gen`"))
        outside = gen.BLOCK_RE.sub("", text)
        for check_id, command in command_texts:
            if command in outside:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: command for check '{check_id}' duplicated "
                                   f"outside a gen block, canon lives in the adapter"))
    return out


def check_instance_leak(root: Path, cfg, framework_dirs: Sequence[str],
                        token_patterns: Sequence[str]) -> list[Finding]:
    out = []
    slugs, _ = unit_slugs(root, cfg.unit_dir)
    slug_res = [(s, re.compile(rf"\b{re.escape(s)}\b")) for s in slugs]
    token_res = [(p, re.compile(p)) for p in token_patterns]
    for fdir in framework_dirs:
        base = Path(root) / fdir
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.stat().st_size > MAX_LEAK_FILE_BYTES:
                continue
            rel = f.relative_to(root).as_posix()
            if rel.startswith(LEAK_EXCLUDE_PREFIXES):
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable: not a doc leak surface
            for slug, rx in slug_res:
                if rx.search(text):
                    out.append(Finding("instance-leak", "gating",
                                       f"{rel}: unit slug '{slug}' leaked into a "
                                       f"framework file"))
            for pat, rx in token_res:
                m = rx.search(text)
                if m:
                    out.append(Finding("instance-leak", "gating",
                                       f"{rel}: instance token '{m.group(0)}' "
                                       f"(pattern {pat})"))
    return out


def check_committed_state(root: Path) -> list[Finding]:
    return [Finding("committed-state", "gating",
                    f"{t} is git-tracked runtime state, gitignore it and "
                    f"`git rm --cached`")
            for t in reap.committed_state(root, list(reap.DEFAULT_PATTERNS))]


def check_brand_literal(root: Path) -> list[Finding]:
    out = []
    for d in BRAND_SCAN_DIRS:
        base = Path(root) / d
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*.py")):
            rel = f.relative_to(root).as_posix()
            if rel in BRAND_EXEMPT or "__pycache__" in f.parts:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
            for n, line in enumerate(text.splitlines(), 1):
                if _BRAND_RE.search(line):
                    out.append(Finding("brand-literal", "gating",
                                       f"{rel}:{n} spells the product name; "
                                       f"import it from core.brand"))
    return out


# ---------------------------------------------------------------- WARNING

def check_stale_state(root: Path, fix: bool, max_age_days: float = 7) -> list[Finding]:
    out = []
    for f in reap.stale_files(root, list(reap.DEFAULT_PATTERNS), max_age_days):
        rel = f.relative_to(root)
        if fix:
            f.unlink()
            print(f"  fixed: deleted {rel}")
        else:
            out.append(Finding("stale-state", "warning",
                               f"{rel} exceeds {max_age_days:g}d (--fix deletes)"))
    return out


def check_unit_slugs(root: Path, cfg, min_len: int) -> list[Finding]:
    _, warnings = unit_slugs(root, cfg.unit_dir, min_len)
    return [Finding("unit-slug", "warning", w) for w in warnings]


# ---------------------------------------------------------------- ADVISORY

def check_honor_rules(root: Path) -> list[Finding]:
    out = []
    for doc in iter_docs(root):
        rel = doc.relative_to(root)
        for n, line in _doc_lines(doc):
            if HONOR_RE.search(line) and PATHISH_RE.search(line):
                out.append(Finding("honor-rules-audit", "advisory",
                                   f"{rel}:{n} un-gated rule prose, promotion "
                                   f"variant: {line.strip()[:80]}"))
    return out


# ---------------------------------------------------------------- driver

def is_template(root) -> bool:
    """The template root: no harness/adapter/adapter.yaml at all. A stamped project
    always has one, so a missing file marks the template; an invalid one does not."""
    return not (Path(root) / config.ADAPTER_REL).exists()


def template_config(root) -> config.Config:
    """The empty configuration template mode lints against: no gates, no checks, the
    default unit directory. Project-only checks are skipped (see PROJECT_ONLY_CHECKS)."""
    return config.Config(root=Path(root), schema=2, project="template", unit_noun="unit",
                         unit_dir="units", packs=(), checks=(), gates=())


PROJECT_ONLY_CHECKS = ("adapter-complete",)


def run_lint(root, cfg=None, *, tier: str = "all", fix: bool = False,
             min_slug_len: int = MIN_SLUG_LEN,
             framework_dirs: Sequence[str] = DEFAULT_FRAMEWORK_DIRS,
             token_patterns: Sequence[str] = ()) -> list[Finding]:
    """Run the selected tiers. cfg is a core.kernel.config.Config; when None the
    configuration is loaded from root, and an invalid one is a gating finding."""
    root = Path(root)
    template = is_template(root)
    if cfg is None:
        if template:
            cfg = template_config(root)
        else:
            try:
                cfg = config.load(root)
            except config.ConfigError as exc:
                return [Finding("adapter-complete", "gating", str(exc))]
    findings: list[Finding] = []
    if tier in ("gating", "all"):
        findings += check_doc_cmd_drift(root, cfg)
        findings += check_orphan_refs(root, cfg)
        findings += check_dup_canon(root, cfg)
        findings += check_instance_leak(root, cfg, framework_dirs, token_patterns)
        findings += check_committed_state(root)
        findings += check_brand_literal(root)
        if not template:
            findings += [Finding("adapter-complete", "gating", msg)
                         for msg in claude_code.check_registration(root)]
    if tier in ("warning", "all"):
        findings += check_stale_state(root, fix)
        findings += check_unit_slugs(root, cfg, min_slug_len)
    if tier in ("advisory", "all"):
        findings += check_honor_rules(root)
    return findings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Harness self-lint.")
    p.add_argument("--tier", choices=["gating", "warning", "advisory", "all"],
                   default="all")
    p.add_argument("--machine", action="store_true",
                   help="one `check<TAB>tier<TAB>message` line per finding")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_root()

    findings = run_lint(root, tier=args.tier, fix=args.fix)
    note = (f"selflint: template mode (no {config.ADAPTER_REL.as_posix()}): skipped "
            f"project-only checks: {', '.join(PROJECT_ONLY_CHECKS)}")
    if is_template(root):
        print(note, file=sys.stderr if args.machine else sys.stdout)
    by_tier = {"gating": [], "warning": [], "advisory": []}
    for f in findings:
        by_tier[f.tier].append(f)
    if args.machine:
        for f in findings:
            print(f"{f.check}\t{f.tier}\t{f.message}")
    else:
        marks = {"gating": "x", "warning": "!", "advisory": "."}
        for tier_name, items in by_tier.items():
            if items:
                print(f"{tier_name.upper()} ({len(items)}):")
                for f in items:
                    print(f"  {marks[tier_name]} [{f.check}] {f.message}")
        if not findings:
            print(f"selflint: clean ({args.tier}).")
    if by_tier["gating"]:
        return 2
    if by_tier["warning"]:
        return 1
    return 0


if __name__ == "__main__":  # run from the repository root: python3 -m core.kernel.selflint
    sys.exit(main())
