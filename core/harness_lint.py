#!/usr/bin/env python3
"""harness_lint.py, the three-tier harness self-check (plan §10).

Tiers, by detection reliability (so trust stays calibrated):
  GATING   deterministic, zero-false-positive, fails the build (exit 2):
           doc-cmd-drift, orphan-refs, dup-canon, adapter-complete,
           instance-leak, committed-state, rule-coverage
  WARNING  heuristic, low-medium FP, reported, exit 1:
           stale-state (--fix deletes), core-drift, handoff-decay
  ADVISORY heuristic, medium-high FP, a human-reviewed worklist, never
           affects the exit code: honor-rules-audit

Usage: harness_lint.py [--tier gating|warning|advisory|all] [--machine]
                       [--fix] [--baseline FILE] [--root DIR]
--machine prints one `check<TAB>tier<TAB>message` line per finding.
--baseline suppresses exact `check<TAB>message` lines listed in FILE
(incremental adoption for the heuristic tiers).
"""
from __future__ import annotations

import argparse
import json
import re
from fnmatch import fnmatch
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, gen, handoff, paths, policy, reap  # noqa: E402

DOC_GLOBS = ["CLAUDE.md", "README.md", "MEMORY.md", "docs/**/*.md",
             ".claude/**/*.md"]
HARNESS_PATH_RE = re.compile(r"harness/[\w./-]+?\.(?:py|sh)\b")
MAKE_TARGET_RE = re.compile(r"\bmake ([a-z][\w-]*)")
CODE_SPAN_RE = re.compile(r"`([^`]*)`")
GATE_NAME_RE = re.compile(r"(?:approve|--set-gate)\s+([a-z][a-z0-9_-]*)")
MD_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
RULE_ANNOT_RE = re.compile(r"<!--\s*rule:([\w-]+)\s*-->")
HOOK_FILES = ["pretool_guard.py", "posttool_lint.py", "stop_gate.py",
              "session_start.py"]
LEAK_EXCLUDE_PREFIXES = ("harness/state/", "harness/logs/", "harness/.fh/",
                         "harness/core/")
LEAK_EXCLUDE_FILES = {"harness/adapter/adapter.yaml"}
MAX_LEAK_FILE_BYTES = 1_000_000


class Finding:
    __slots__ = ("check", "tier", "message")

    def __init__(self, check: str, tier: str, message: str):
        self.check, self.tier, self.message = check, tier, message


def iter_docs(root: Path, cfg=None) -> list[Path]:
    """The living-doc set: built-in globs, extended/pruned by adapter
    `docs_lint: {extra_globs, exclude_globs}` (e.g. pull profile docs in,
    keep superseded planning artifacts out)."""
    docs_lint = (getattr(cfg, "docs_lint", None) or {}) if cfg else {}
    extra = docs_lint.get("extra_globs") or []
    exclude = docs_lint.get("exclude_globs") or []
    seen, out = set(), []
    for pat in [*DOC_GLOBS, *extra]:
        for f in sorted(root.glob(pat)):
            if not f.is_file() or f in seen:
                continue
            seen.add(f)
            rel = f.relative_to(root).as_posix()
            if any(fnmatch(rel, e) for e in exclude):
                continue
            out.append(f)
    return out


def _doc_lines(doc: Path):
    for n, line in enumerate(
            doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        yield n, line


def _make_targets(root: Path) -> set:
    mk = root / "Makefile"
    if not mk.is_file():
        return set()
    return {m.group(1) for m in
            re.finditer(r"^([A-Za-z][\w-]*):", mk.read_text(errors="replace"),
                        re.M)}


# ---------------------------------------------------------------- GATING

def check_doc_cmd_drift(root: Path, cfg) -> list[Finding]:
    out = []
    targets = _make_targets(root)
    for doc in iter_docs(root, cfg):
        rel = doc.relative_to(root)
        in_fence = False
        for n, line in _doc_lines(doc):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            for m in HARNESS_PATH_RE.finditer(line):
                if not (root / m.group(0)).is_file():
                    out.append(Finding("doc-cmd-drift", "gating",
                                       f"{rel}:{n} references missing {m.group(0)}"))
            # `make X` is also an English phrase ("make it work"), only a
            # code context (fenced block or inline code span) is a command.
            code = [line] if in_fence else CODE_SPAN_RE.findall(line)
            for seg in code:
                for m in MAKE_TARGET_RE.finditer(seg):
                    if m.group(1) not in targets:
                        out.append(Finding("doc-cmd-drift", "gating",
                                           f"{rel}:{n} references missing make "
                                           f"target '{m.group(1)}'"))
            for m in GATE_NAME_RE.finditer(line):
                if m.group(1) not in cfg.gates:
                    out.append(Finding("doc-cmd-drift", "gating",
                                       f"{rel}:{n} names unknown gate "
                                       f"'{m.group(1)}'"))
    return out


def check_orphan_refs(root: Path, cfg) -> list[Finding]:
    out = []
    docs = iter_docs(root, cfg)
    stems = {f.stem for f in docs}
    for doc in docs:
        rel = doc.relative_to(root)
        for n, line in _doc_lines(doc):
            for m in MD_LINK_RE.finditer(line):
                href = m.group(1).split("#")[0].strip()
                if (not href or "://" in href or href.startswith("mailto:")
                        or "{" in href):
                    continue
                if not ((doc.parent / href).exists() or (root / href).exists()):
                    out.append(Finding("orphan-refs", "gating",
                                       f"{rel}:{n} broken link -> {href}"))
            for m in WIKILINK_RE.finditer(line):
                if m.group(1).strip() not in stems:
                    out.append(Finding("orphan-refs", "gating",
                                       f"{rel}:{n} unresolved wikilink "
                                       f"[[{m.group(1).strip()}]]"))
    rubric = cfg.reviewer.get("rubric")
    if rubric and not (root / rubric).is_file():
        out.append(Finding("orphan-refs", "gating",
                           f"adapter reviewer.rubric missing: {rubric}"))
    for check in cfg.checks:
        for m in HARNESS_PATH_RE.finditer(str(check.get("cmd", ""))):
            if not (root / m.group(0)).is_file():
                out.append(Finding("orphan-refs", "gating",
                                   f"check '{check.get('id')}' cmd references "
                                   f"missing {m.group(0)}"))
    if cfg.loop.get("enabled"):
        spec = str(cfg.loop.get("oracle") or "")
        module, _, cls = spec.partition(":")
        # A core-shipped oracle (e.g. core.loop.toy_oracle) needs no project file;
        # core-drift owns its integrity.
        if not module.startswith("core.loop."):
            oracle_file = root / Path(*module.split(".")).with_suffix(".py")
            if not oracle_file.is_file():
                out.append(Finding("orphan-refs", "gating",
                                   f"loop enabled but oracle module missing: "
                                   f"{oracle_file.relative_to(root)}"))
            elif cls and f"class {cls}" not in oracle_file.read_text(errors="replace"):
                out.append(Finding("orphan-refs", "gating",
                                   f"loop oracle {oracle_file.relative_to(root)} "
                                   f"defines no class {cls}"))
    return out


def check_dup_canon(root: Path, cfg) -> list[Finding]:
    out = []
    for doc in gen.iter_gen_docs(root):
        if doc.name == "HANDOFF.md":
            continue  # session state, not canon
        rel = doc.relative_to(root)
        text = doc.read_text(encoding="utf-8", errors="replace")
        problem = gen.check_markers(text)
        if problem:
            out.append(Finding("dup-canon", "gating",
                               f"{rel}: {problem}, regeneration refuses this "
                               f"file until the markers are repaired"))
            continue
        for m in gen.BLOCK_RE.finditer(text):
            name, body = m.group(2), m.group(3).strip("\n")
            rendered = gen.render_block(name, cfg)
            if rendered is None:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: unknown fh:gen block '{name}'"))
            elif body != rendered:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: fh:gen block '{name}' does not match "
                                   f"regeneration: run `python3 "
                                   f"harness/core/gen.py` (F1: a stamped "
                                   f"project has no fh)"))
        outside = gen.BLOCK_RE.sub("", text)
        for gate, cmd in cfg.gate_commands.items():
            if str(cmd) and str(cmd) in outside:
                out.append(Finding("dup-canon", "gating",
                                   f"{rel}: gate command for '{gate}' duplicated "
                                   f"outside a fh:gen block, canon lives in "
                                   f"adapter.yaml only"))
    return out


def check_adapter_complete(root: Path, cfg) -> list[Finding]:
    out = []
    settings = root / ".claude" / "settings.json"
    if not settings.is_file():
        out.append(Finding("adapter-complete", "gating",
                           ".claude/settings.json missing (hooks unregistered)"))
    else:
        try:
            blob = json.dumps(json.loads(settings.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            blob = ""
            out.append(Finding("adapter-complete", "gating",
                               f".claude/settings.json unreadable: {exc}"))
        for hook in HOOK_FILES:
            if f"harness/core/hooks/{hook}" not in blob:
                out.append(Finding("adapter-complete", "gating",
                                   f"hook not registered in settings.json: {hook}"))
        if blob and "--bash-relint" not in blob:
            out.append(Finding("adapter-complete", "gating",
                               "posttool_lint --bash-relint (Bash matcher) not "
                               "registered: the sed/tee bypass is open"))
    for problem in handoff.validate(root):
        out.append(Finding("adapter-complete", "gating", f"HANDOFF: {problem}"))
    return out


def check_instance_leak(root: Path, cfg) -> list[Finding]:
    out = []
    units = root / cfg.unit_dir
    slugs = sorted(d.name for d in units.iterdir()
                   if units.is_dir() and d.is_dir()) if units.is_dir() else []
    slug_res = [(s, re.compile(rf"\b{re.escape(s)}\b")) for s in slugs]
    token_res = [(p, re.compile(p)) for p in cfg.instance_token_patterns]
    for fdir in cfg.framework_dirs:
        base = root / fdir
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if not f.is_file() or f.stat().st_size > MAX_LEAK_FILE_BYTES:
                continue
            rel = f.relative_to(root).as_posix()
            if rel.startswith(LEAK_EXCLUDE_PREFIXES) or rel in LEAK_EXCLUDE_FILES:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable, not a doc leak surface
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


def check_committed_state(root: Path, cfg) -> list[Finding]:
    patterns = (cfg.state_reap.get("patterns") or []) + ["*.pid", "**/*.pid"]
    return [Finding("committed-state", "gating",
                    f"{t} is git-tracked runtime state, gitignore it and "
                    f"`git rm --cached`")
            for t in reap.committed_state(root, patterns)]


def check_rule_coverage(root: Path, cfg) -> list[Finding]:
    annotated = set()
    for doc in iter_docs(root, cfg):
        annotated |= {m.group(1) for m in
                      RULE_ANNOT_RE.finditer(doc.read_text(errors="replace"))}
    enforced = {p.get("enforces") for p in cfg.deny if p.get("enforces")}
    enforced |= {c.get("enforces") for c in cfg.checks if c.get("enforces")}
    out = [Finding("rule-coverage", "gating",
                   f"rule '{r}' is annotated but nothing declares "
                   f"`enforces: {r}`. Promote it to a check or deny policy")
           for r in sorted(annotated - enforced)]
    out += [Finding("rule-coverage", "gating",
                    f"`enforces: {e}` has no live <!-- rule:{e} --> annotation "
                    f", orphaned gate (delete it or re-annotate the rule)")
            for e in sorted(enforced - annotated)]
    return out


# ---------------------------------------------------------------- WARNING

def check_stale_state(root: Path, cfg, fix: bool) -> list[Finding]:
    patterns = cfg.state_reap.get("patterns") or []
    max_age = cfg.state_reap.get("max_age_days", 7)
    out = []
    for f in reap.stale_files(root, patterns, max_age):
        rel = f.relative_to(root)
        if fix:
            f.unlink()
            print(f"  fixed: deleted {rel}")
        else:
            out.append(Finding("stale-state", "warning",
                               f"{rel} exceeds {max_age}d (--fix deletes)"))
    return out


def check_core_drift(root: Path, cfg) -> list[Finding]:
    from core.manifest import sha256_file
    mpath = root / "harness" / ".fh" / "MANIFEST.json"
    if not mpath.is_file():
        return []  # template checkout / unstamped tree, nothing to compare
    try:
        files = json.loads(mpath.read_text()).get("files", {})
    except (json.JSONDecodeError, OSError) as exc:
        return [Finding("core-drift", "warning", f"MANIFEST.json unreadable: {exc}")]
    out = []
    for rel, digest in files.items():
        p = root / rel
        if not p.is_file():
            out.append(Finding("core-drift", "warning",
                               f"{rel} missing (recorded in MANIFEST), run "
                               f"`gatebench update`"))
        elif sha256_file(p) != digest:
            out.append(Finding("core-drift", "warning",
                               f"{rel} differs from its stamp: `gatebench upstream` "
                               f"the improvement or `gatebench update` to restore"))
    return out


def check_handoff_decay(root: Path, cfg) -> list[Finding]:
    n = handoff.trailing_stub_count(root)
    if n >= 2:
        return [Finding("handoff-decay", "warning",
                        f"{n} consecutive auto-handoff stubs, nobody is "
                        f"consolidating (PROTOCOL.md §5)")]
    return []


# ---------------------------------------------------------------- ADVISORY

HONOR_RE = re.compile(r"\b(never|always|must)\b", re.I)
# a path-like token: word chars then a slash (advisory tier tolerates FPs)
PATHISH_RE = re.compile(r"\b[\w.-]+/[\w./*-]*")


def check_honor_rules(root: Path, cfg) -> list[Finding]:
    out = []
    for doc in iter_docs(root, cfg):
        rel = doc.relative_to(root)
        prev = ""
        for n, line in _doc_lines(doc):
            if (HONOR_RE.search(line) and PATHISH_RE.search(line)
                    and not RULE_ANNOT_RE.search(line)
                    and not RULE_ANNOT_RE.search(prev)):
                out.append(Finding("honor-rules-audit", "advisory",
                                   f"{rel}:{n} un-gated rule prose, promotion "
                                   f"variant: {line.strip()[:80]}"))
            prev = line
    return out


# ---------------------------------------------------------------- driver

def run(root: Path, tier: str, fix: bool) -> list[Finding]:
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        return [Finding("adapter-complete", "gating", f"adapter.yaml invalid: {exc}")]

    findings: list[Finding] = []
    if tier in ("gating", "all"):
        for chk in (check_doc_cmd_drift, check_orphan_refs, check_dup_canon,
                    check_adapter_complete, check_instance_leak,
                    check_committed_state, check_rule_coverage):
            findings += chk(root, cfg)
    if tier in ("warning", "all"):
        findings += check_stale_state(root, cfg, fix)
        findings += check_core_drift(root, cfg)
        findings += check_handoff_decay(root, cfg)
    if tier in ("advisory", "all"):
        findings += check_honor_rules(root, cfg)
    return findings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tier", choices=["gating", "warning", "advisory", "all"],
                   default="all")
    p.add_argument("--machine", action="store_true")
    p.add_argument("--fix", action="store_true")
    p.add_argument("--baseline")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()

    findings = run(root, args.tier, args.fix)
    if args.baseline:
        try:
            suppressed = set(Path(args.baseline).read_text().splitlines())
        except OSError:
            suppressed = set()
        findings = [f for f in findings
                    if f"{f.check}\t{f.message}" not in suppressed]

    by_tier = {"gating": [], "warning": [], "advisory": []}
    for f in findings:
        by_tier[f.tier].append(f)

    if args.machine:
        for f in findings:
            print(f"{f.check}\t{f.tier}\t{f.message}")
    else:
        marks = {"gating": "✗", "warning": "!", "advisory": "·"}
        for tier_name, items in by_tier.items():
            if items:
                print(f"{tier_name.upper()} ({len(items)}):")
                for f in items:
                    print(f"  {marks[tier_name]} [{f.check}] {f.message}")
        if not findings:
            print(f"harness-lint: clean ({args.tier}).")

    if by_tier["gating"]:
        return 2
    if by_tier["warning"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
