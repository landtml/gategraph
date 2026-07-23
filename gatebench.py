#!/usr/bin/env python3
"""gatebench: Gatebench template stamper and sync tool.

Subcommands (plan §4):
  init <dir>      questionnaire + stamp core/ + seed/ + MANIFEST.json + git commit
  update <dir>    (P3) overwrite pristine core files; .fhnew + drift report otherwise
  diff <dir>      (P3) report core drift only
  upstream <dir> <file>   (P3) promote a project-modified core file to the template
  gen <dir>       (P3) regenerate <!-- fh:gen --> derived doc blocks

stdlib + PyYAML only. Core files are stamped verbatim (hash-tracked, fh-owned);
seed files are rendered once at init and never touched again (adapter-owned).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

TEMPLATE_ROOT = Path(__file__).resolve().parent
CORE_SRC = TEMPLATE_ROOT / "core"
SEED_SRC = TEMPLATE_ROOT / "seed"

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
DEFAULT_GATES = ["composed", "checked", "verified"]
FIVE_GATE_PRESET = ["scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"]
ACCT_DENY = ("    - {id: acct, regex: '\\d{6}\\s*-?\\s*\\d{4}', scope: [any], "
            "summary: \"no account number on disk\", enforces: no-acct}")
ACCT_RULE = ("<!-- rule:no-acct -->\n"
            "- Never write a account number number to disk anywhere in this repo.\n"
            "  Enforced by the `acct` deny policy in `harness/adapter/adapter.yaml`.")

# seed source -> destination relative to the stamped project root.
# {project} is substituted; .tmpl sources are token-rendered, others copied as-is
# (all pass through render(), which is a no-op without tokens).
SEED_MAP = {
    "CLAUDE.md.tmpl": "CLAUDE.md",
    "workflow.md.tmpl": ".claude/skills/{project}/workflow.md",
    "SKILL.md.tmpl": ".claude/skills/{project}/SKILL.md",
    "HANDOFF.md": "HANDOFF.md",
    "PROGRESS.yaml": "PROGRESS.yaml",
    "MEMORY.md": "MEMORY.md",
    "Makefile": "Makefile",
    "adapter.yaml": "harness/adapter/adapter.yaml",
    "review_rubric.md": "harness/adapter/review_rubric.md",
    "checks/verify_claims.py": "harness/adapter/checks/verify_claims.py",
    "dot_claude/settings.json": ".claude/settings.json",
    "dot_claude/agents/reviewer.md": ".claude/agents/reviewer.md",
}
GITIGNORE_SEED = "dot_gitignore"
ORACLE_SEED = "oracle.py.tmpl"  # stamped only when loop enabled
EMPTY_DIRS = ["harness/adapter/checks", "harness/eval", "harness/state", "harness/logs"]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def template_version() -> str:
    return (TEMPLATE_ROOT / "VERSION").read_text().strip()


def die(msg: str, code: int = 1) -> "NoReturn":
    print(f"gatebench: error: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- answers

def _parse_gate_list(raw, field: str) -> list:
    if isinstance(raw, str):
        raw = [g.strip() for g in raw.split(",") if g.strip()]
    for g in raw:
        if not SLUG_RE.match(g):
            die(f"{field}: invalid gate name {g!r} (want lowercase slug)")
    if len(raw) != len(set(raw)):
        die(f"{field}: duplicate gate names in {raw}")
    return list(raw)


def normalize_answers(a: dict, target: Path) -> dict:
    """Validate and default-fill an answers dict (from file or questionnaire)."""
    out = {}
    out["project"] = str(a.get("project") or target.name).strip().lower().replace(" ", "-")
    if not SLUG_RE.match(out["project"]):
        die(f"project name {out['project']!r} must be a lowercase slug")
    out["artifact_noun"] = str(a.get("artifact_noun") or "unit").strip()
    out["unit_dir"] = str(a.get("unit_dir") or "units").strip()
    gates = a.get("gates") or DEFAULT_GATES
    if isinstance(gates, str) and gates.strip().lower() == "five-gate":
        gates = FIVE_GATE_PRESET
    out["gates"] = _parse_gate_list(gates, "gates")
    hashed = a.get("hashed_gates") or []
    out["hashed_gates"] = _parse_gate_list(hashed, "hashed_gates")
    unknown = set(out["hashed_gates"]) - set(out["gates"])
    if unknown:
        die(f"hashed_gates not in gates: {sorted(unknown)}")
    out["seed_deny"] = bool(a.get("seed_deny", True))
    out["loop_enabled"] = bool(a.get("loop_enabled", False))
    return out


def questionnaire(target: Path) -> dict:
    """The ~7-prompt init questionnaire (§6). Everything else is edited in
    adapter.yaml later."""
    print("gatebench init: questionnaire (Enter accepts the [default])")

    def ask(q, d):
        try:
            return input(f"  {q} [{d}]: ").strip() or d
        except EOFError:
            # Agents and CI pipe stdin; when it runs dry mid-questionnaire the
            # raw EOFError traceback observed in the wild helps nobody.
            die(f"stdin ended before the questionnaire finished (at: {q}). "
                f"Use `--defaults` (accept every default; project = dir "
                f"name) or `--answers FILE` (YAML with the questionnaire keys)")
    a = {}
    a["project"] = ask("project name", target.name)
    a["artifact_noun"] = ask("artifact noun (what is one unit of work?)", "unit")
    a["unit_dir"] = ask("unit directory", "units")
    a["gates"] = ask("gate chain, comma-separated ('five-gate' = 5-gate preset)",
                     ",".join(DEFAULT_GATES))
    a["hashed_gates"] = ask("which gates are sha256-hashed (staleness-tracked)?", "")
    a["seed_deny"] = ask("seed the example deny-regex (account-number guard)? y/n", "y") \
        .lower().startswith("y")
    a["loop_enabled"] = ask("enable the autonomous loop now (Layer 2)? y/n", "n") \
        .lower().startswith("y")
    return a


# ---------------------------------------------------------------- rendering

def gate_command(gate: str, tool_set: list) -> str:
    if gate in tool_set:
        # earned from the lint runner (deny policies + adapter checks), which
        # records the sha256 evidence map; tool-set gates refuse manual approve
        return f"python3 harness/core/lint.py --unit {{slug}} --set-gate {gate}"
    return f"python3 harness/core/gate.py approve {gate} --unit {{slug}}"


def render_gate_commands(answers: dict) -> str:
    tool_set = answers["hashed_gates"]
    lines = []
    for g in answers["gates"]:
        pad = " " * max(1, 14 - len(g))
        lines.append(f'    {g}:{pad}"{gate_command(g, tool_set)}"')
    return "\n".join(lines)


def render_policies_block(answers: dict) -> str:
    deny = f"  deny:\n{ACCT_DENY}" if answers["seed_deny"] else "  deny: []"
    return f"{deny}\n  ask: []"


def yaml_flow(items: list) -> str:
    return "[" + ", ".join(items) + "]"


def render(text: str, answers: dict) -> str:
    tokens = {
        "FH_PROJECT": answers["project"],
        "FH_ARTIFACT_NOUN": answers["artifact_noun"],
        "FH_UNIT_DIR": answers["unit_dir"],
        "FH_GATES": yaml_flow(answers["gates"]),
        "FH_HASHED_GATES": yaml_flow(answers["hashed_gates"]),
        "FH_TOOL_SET_GATES": yaml_flow(answers["hashed_gates"]),
        "FH_GATE_COMMANDS": render_gate_commands(answers),
        "FH_POLICIES_BLOCK": render_policies_block(answers),
        "FH_LOOP_ENABLED": "true" if answers["loop_enabled"] else "false",
        "FH_RULES_BLOCK": ACCT_RULE if answers["seed_deny"] else
            "(no annotated rules yet, promote enforceable rules into\n"
            "`harness/adapter/adapter.yaml`, then tag them here with a\n"
            "`rule:<id>` comment annotation so rule-coverage lint can verify\n"
            "the linkage deterministically)",
    }
    for name, value in tokens.items():
        text = text.replace("{{" + name + "}}", value)
    leftover = re.search(r"\{\{FH_[A-Z_]+\}\}", text)
    if leftover:
        die(f"unrendered template token {leftover.group(0)}")
    return text


# ---------------------------------------------------------------- core access
# gatebench.py loads config.py/gen.py straight from a core checkout (the template's
# for init, the project's for gen) with importlib, so the two never collide
# in one process and each project is served by its own stamped core version.

def _load_core_module(core_dir: Path, name: str):
    spec = importlib.util.spec_from_file_location(f"_fh_{name}",
                                                  core_dir / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def gen_project(target: Path, core_dir: Path) -> list:
    """Regenerate all <!-- fh:gen --> blocks in a project's docs."""
    cfg = _load_core_module(core_dir, "config").load(target)
    gen_mod = _load_core_module(core_dir, "gen")
    try:
        return gen_mod.regenerate_project(target, cfg)
    except getattr(gen_mod, "GenError", ()) as exc:  # older cores: no GenError
        die(f"gatebench gen refused: {exc}")


# ---------------------------------------------------------------- stamping

def write_rendered(src: Path, dst: Path, answers: dict) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(src.read_text(), answers))
    shutil.copymode(src, dst)


def stamp_core(target: Path) -> dict:
    """Copy core/ verbatim; return {relpath: sha256} for the manifest."""
    dst_root = target / "harness" / "core"
    shutil.copytree(CORE_SRC, dst_root,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return {
        str(p.relative_to(target)): sha256_file(p)
        for p in sorted(dst_root.rglob("*")) if p.is_file()
    }


def seed_targets(answers: dict) -> "list[str]":
    """Project-relative paths the seed stamp would write (conflict surface)."""
    rels = [dst.format(project=answers["project"]) for dst in SEED_MAP.values()]
    if answers["loop_enabled"]:
        rels.append("harness/adapter/oracle.py")
    return rels  # .gitignore is merge-not-overwrite, so it is never a conflict


def stamp_seed(target: Path, answers: dict,
               retrofit: bool = False) -> "tuple[list[str], list[str]]":
    """Render the seed. Returns (written, preserved) project-relative paths.

    In ``retrofit`` mode an existing destination is NEVER overwritten: the
    rendered template lands beside it as ``<file>.fhnew`` (the fh-update
    convention) and the file is reported as preserved."""
    written: "list[str]" = []
    preserved: "list[str]" = []

    def place(src: Path, rel: str) -> None:
        dst = target / rel
        if retrofit and dst.exists():
            write_rendered(src, dst.with_name(dst.name + ".fhnew"), answers)
            preserved.append(rel)
        else:
            write_rendered(src, dst, answers)
            written.append(rel)

    for src_rel, dst_rel in SEED_MAP.items():
        place(SEED_SRC / src_rel, dst_rel.format(project=answers["project"]))
    if answers["loop_enabled"]:
        place(SEED_SRC / ORACLE_SEED, "harness/adapter/oracle.py")
    for rel in EMPTY_DIRS:
        (target / rel).mkdir(parents=True, exist_ok=True)
    # .gitignore: create, or append our lines to an existing one (retrofit case)
    seed_lines = (SEED_SRC / GITIGNORE_SEED).read_text()
    gitignore = target / ".gitignore"
    if gitignore.exists():
        have = gitignore.read_text()
        missing = [l for l in seed_lines.splitlines() if l and l not in have.splitlines()]
        if missing:
            gitignore.write_text(have.rstrip("\n") + "\n" + "\n".join(missing) + "\n")
    else:
        gitignore.write_text(seed_lines)
    written.append(".gitignore")
    return written, preserved


def write_manifest(target: Path, core_hashes: dict) -> None:
    fh_dir = target / "harness" / ".fh"
    fh_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"version": template_version(), "files": core_hashes}
    (fh_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")


def git_commit_stamp(target: Path, project: str,
                     only: "list[str] | None" = None) -> None:
    """Commit the stamp. ``only`` (retrofit mode) restricts BOTH the add and
    the commit to the paths init created: a dirty repo's untracked, unstaged,
    or even already-staged user files are left exactly as they were."""
    def git(*args, check=True):
        return subprocess.run(["git", "-C", str(target), *args],
                              check=check, capture_output=True, text=True)

    if git("rev-parse", "--git-dir", check=False).returncode != 0:
        git("init", "-b", "main")
    if only is None:
        git("add", "-A")
    else:
        git("add", "--", *only)
    identity = []
    if git("config", "user.email", check=False).returncode != 0:
        identity = ["-c", "user.name=fh-init", "-c", "user.email=fh@localhost"]
    cmd = ["git", "-C", str(target), *identity, "commit", "-m",
           f"gatebench init: stamp Gatebench v{template_version()} ({project})"]
    if only is not None:
        cmd += ["--", *only]  # pathspec commit: user's staged files stay staged
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def prove_stamp_alive(target: Path) -> None:
    if os.environ.get("FH_SKIP_STAMP_SELFCHECK") == "1":
        # F10: test rigs that stamp many projects assert core integrity
        # separately (gatebench diff); real inits keep the self-check default-ON.
        print("  stamp self-check: SKIPPED (FH_SKIP_STAMP_SELFCHECK=1)")
        return
    proc = subprocess.run(["bash", "harness/core/tests/run_checks.sh"],
                          cwd=target, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"stamp self-check failed:\n{proc.stdout}\n{proc.stderr}")
    print("  stamp self-check: GREEN (harness/core/tests/run_checks.sh)")


def _preflight_gitignore(target: Path) -> None:
    """Refuse BEFORE any write when the target's .gitignore would swallow the
    stamp: `git add harness` then fails midway and leaves a partial stamp
    (observed in the wild: a production template copy shipped with the dev
    repo's `/harness/` ignore line)."""
    gi = target / ".gitignore"
    if not gi.exists():
        return
    if (target / ".git").exists():
        # Probe a path INSIDE the dir too: a `/harness/` (dir-only) pattern
        # does not match the bare not-yet-existing "harness" probe. No -q:
        # git only allows it with a single pathname.
        probe = subprocess.run(
            ["git", "-C", str(target), "check-ignore",
             "harness", "harness/core/gate.py"],
            capture_output=True)
        ignored = probe.returncode == 0
    else:
        ignored = any(re.fullmatch(r"/?harness/?\*?", ln.strip())
                      for ln in gi.read_text(encoding="utf-8").splitlines())
    if ignored:
        die(f"{gi} ignores `harness/`: the stamp could not be committed and "
            f"init would die halfway. Delete that line and re-run "
            f"(nothing has been written)")


def cmd_init(args) -> None:
    target = Path(args.dir).resolve()
    if (target / "harness" / ".fh" / "MANIFEST.json").exists():
        die(f"{target} is already stamped (harness/.fh/MANIFEST.json exists); "
            f"use `gatebench update` to sync it")
    if args.answers:
        raw = yaml.safe_load(Path(args.answers).read_text()) or {}
        answers = normalize_answers(raw, target)
    elif getattr(args, "defaults", False):
        answers = normalize_answers({}, target)  # every questionnaire default
    else:
        answers = normalize_answers(questionnaire(target), target)
    _preflight_gitignore(target)

    # F11: refuse BEFORE writing anything when the target already has files
    # the seed would overwrite (the P6 retrofit nearly lost a live HANDOFF.md).
    conflicts = [rel for rel in seed_targets(answers) if (target / rel).exists()]
    if conflicts and not getattr(args, "retrofit", False):
        die("these files already exist and gatebench init would OVERWRITE them:\n"
            + "".join(f"    {r}\n" for r in conflicts)
            + "  re-run with --retrofit to preserve them (template versions land "
            "beside each as .fhnew,\n  and the stamp commit stages ONLY what "
            "init creates; your dirty tree stays yours)")

    target.mkdir(parents=True, exist_ok=True)
    core_hashes = stamp_core(target)
    written, preserved = stamp_seed(target, answers,
                                    retrofit=getattr(args, "retrofit", False))
    write_manifest(target, core_hashes)
    fh_dir = target / "harness" / ".fh"
    (fh_dir / "answers.yaml").write_text(yaml.safe_dump(answers, sort_keys=True))
    changed = gen_project(target, target / "harness" / "core")
    for rel in preserved:
        print(f"  preserved: {rel} (yours untouched; template version at {rel}.fhnew)")
    if getattr(args, "retrofit", False):
        stage = ["harness", ".claude", ".gitignore"] + written
        stage += [str(Path(c).resolve().relative_to(target)) for c in changed]
        git_commit_stamp(target, answers["project"],
                         only=sorted(set(stage)))
    else:
        git_commit_stamp(target, answers["project"])
    prove_stamp_alive(target)
    print(f"gatebench init: stamped {answers['project']} at {target} "
          f"(template v{template_version()}, {len(core_hashes)} core files)")


# ---------------------------------------------------------------- sync

def _template_core_files() -> dict:
    """{relpath-under-core: absolute source Path} for the template's core."""
    return {p.relative_to(CORE_SRC).as_posix(): p
            for p in sorted(CORE_SRC.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


def _manifest_path(target: Path) -> Path:
    return target / "harness" / ".fh" / "MANIFEST.json"


def _read_manifest(target: Path) -> dict:
    path = _manifest_path(target)
    if not path.is_file():
        die(f"{target} is not a stamped project (no {path})")
    try:
        manifest = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        die(f"{path} is corrupt (not valid JSON: {exc}). Restore it from git "
            f"history before syncing; every update rewrites it, so the last "
            f"committed version is authoritative")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), dict):
        die(f"{path} is malformed (no 'files' map). Restore it from git history")
    return manifest


def _stamped_target(arg_dir: str) -> Path:
    return Path(arg_dir).resolve()


def cmd_update(args) -> None:
    """Per core file: pristine -> overwrite + re-record; modified -> .fhnew +
    drift report (never silently clobber); removed upstream -> reap if pristine."""
    target = _stamped_target(args.dir)
    manifest = _read_manifest(target)
    files = manifest["files"]
    template = _template_core_files()
    updated, created, drifted, removed, resolved = [], [], [], [], []

    def clear_sidecar(dst: Path, key: str) -> None:
        # The base file matches the template again: a lingering .fhnew is a
        # resolved drift's leftover (gitignored, so nothing else surfaces it).
        side = dst.with_name(dst.name + ".fhnew")
        if side.exists():
            side.unlink()
            resolved.append(key)

    for rel, src in template.items():
        key = f"harness/core/{rel}"
        dst = target / key
        src_bytes = src.read_bytes()
        src_hash = hashlib.sha256(src_bytes).hexdigest()
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src_bytes)
            shutil.copymode(src, dst)
            files[key] = src_hash
            created.append(key)
            continue
        dst_hash = sha256_file(dst)
        if dst_hash == src_hash:
            files[key] = src_hash  # already current (or hotfix == upstream)
            clear_sidecar(dst, key)
            continue
        if files.get(key) == dst_hash:  # pristine at the old version
            dst.write_bytes(src_bytes)
            shutil.copymode(src, dst)
            files[key] = src_hash
            updated.append(key)
            clear_sidecar(dst, key)
        else:  # locally modified: never clobber
            dst.with_name(dst.name + ".fhnew").write_bytes(src_bytes)
            drifted.append(key)

    for key in [k for k in files if k.startswith("harness/core/")]:
        if key.removeprefix("harness/core/") in template:
            continue  # still shipped
        dst = target / key
        if not dst.exists() or sha256_file(dst) == files[key]:
            dst.unlink(missing_ok=True)
            del files[key]
            removed.append(key)
        else:
            drifted.append(f"{key} (removed upstream but locally modified, kept)")

    manifest["version"] = template_version()
    _manifest_path(target).write_text(json.dumps(manifest, indent=2) + "\n")

    for label, items in (("updated", updated), ("new", created),
                         ("removed", removed)):
        for k in items:
            print(f"  {label}: {k}")
    for k in resolved:
        print(f"  resolved: {k} (.fhnew removed, base file matches the template again)")
    if drifted:
        print(f"\ngatebench update: DRIFT: {len(drifted)} locally modified file(s) "
              f"NOT overwritten (template version saved as .fhnew):")
        for k in drifted:
            print(f"  ! {k}")
        print("  resolve each: merge the .fhnew by hand, or promote your edit "
              "with `gatebench upstream`.")
    print(f"gatebench update: {target.name} -> v{template_version()} "
          f"({len(updated)} updated, {len(created)} new, {len(removed)} removed, "
          f"{len(drifted)} drifted)")


def cmd_diff(args) -> None:
    """Report drift only, no writes. Covers BOTH directions: manifest files
    that drifted locally AND template files the manifest has never seen
    (upstream additions `gatebench update` would stamp)."""
    target = _stamped_target(args.dir)
    files = _read_manifest(target)["files"]
    template = _template_core_files()
    modified, missing, outdated = [], [], []
    for key, recorded in files.items():
        dst = target / key
        if not dst.exists():
            missing.append(key)
            continue
        if sha256_file(dst) != recorded:
            modified.append(key)
            continue
        src = template.get(key.removeprefix("harness/core/"))
        if src is not None and sha256_file(src) != recorded:
            outdated.append(key)
    new_upstream = [f"harness/core/{rel}" for rel in template
                    if f"harness/core/{rel}" not in files]
    for k in modified:
        print(f"  modified: {k}  (promote: gatebench upstream, or restore: gatebench update)")
    for k in missing:
        print(f"  missing:  {k}  (restore: gatebench update)")
    for k in outdated:
        print(f"  outdated: {k}  (template has a newer version: gatebench update)")
    for k in new_upstream:
        print(f"  new upstream: {k}  (gatebench update will add it)")
    if not (modified or missing or outdated or new_upstream):
        print("gatebench diff: core is pristine and current.")
    if modified or missing:
        sys.exit(1)


def cmd_upstream(args) -> None:
    """Promote a project-modified core file back into the template repo:
    how a harness improvement grown in one project flows to all of them."""
    target = _stamped_target(args.dir)
    manifest = _read_manifest(target)
    rel = args.file
    for prefix in ("harness/core/", "core/"):
        rel = rel.removeprefix(prefix)
    src = target / "harness" / "core" / rel
    if not src.is_file():
        die(f"no such core file in project: harness/core/{rel}")
    dst = CORE_SRC / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    # the project copy now matches the template again: re-record it
    manifest["files"][f"harness/core/{rel}"] = sha256_file(src)
    _manifest_path(target).write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"gatebench upstream: core/{rel} promoted to the template.\n"
          f"  next: run the template test suite, bump VERSION, note it in "
          f"CHANGELOG.md, then `gatebench update` your other projects.")


def cmd_gen(args) -> None:
    target = _stamped_target(args.dir)
    core_dir = target / "harness" / "core"
    if not core_dir.is_dir():
        die(f"{target} has no harness/core (not a stamped project)")
    changed = gen_project(target, core_dir)
    for f in changed:
        print(f"  regenerated: {f.relative_to(target)}")
    print(f"gatebench gen: {len(changed)} file(s) regenerated.")


# ------------------------------------------------- config editing (F1+F8)
# adapter.yaml is the single source of truth AND its own documentation, so
# programmatic edits must be targeted TEXT operations (yaml.safe_dump round-
# trips destroy every comment, F8) and must regenerate the gen blocks in the
# same step (an edit without regeneration leaves dup-canon broken, F1).

def _project_core(target: Path) -> Path:
    core_dir = target / "harness" / "core"
    if not core_dir.is_dir():
        die(f"{target} has no harness/core (not a stamped project)")
    return core_dir


def _load_project_cfg(target: Path, core_dir: Path):
    try:
        return _load_core_module(core_dir, "config").load(target)
    except Exception as exc:
        die(f"cannot load {target}/harness/adapter/adapter.yaml: {exc}")


def _rule_annotated(target: Path, cfg, rule: str) -> bool:
    """Is <!-- rule:X --> live anywhere in the project's doc set? Mirrors
    harness_lint's rule-coverage globs (plus adapter docs_lint.extra_globs)."""
    globs = ["CLAUDE.md", "README.md", "MEMORY.md", "docs/**/*.md",
             ".claude/**/*.md"] + list(cfg.docs_lint.get("extra_globs") or [])
    annot = re.compile(rf"<!--\s*rule:{re.escape(rule)}\s*-->")
    return any(f.is_file() and annot.search(f.read_text(encoding="utf-8",
                                                        errors="replace"))
               for pat in globs for f in target.glob(pat))


def _rewrite_validate_regen(target: Path, core_dir: Path, new_text: str,
                            action: str) -> None:
    """Write adapter.yaml, re-validate with the project's OWN config loader
    (roll back on failure), then regenerate gen blocks in the same step."""
    apath = target / "harness" / "adapter" / "adapter.yaml"
    before = apath.read_text(encoding="utf-8")
    apath.write_text(new_text, encoding="utf-8")
    try:
        _load_core_module(core_dir, "config").load(target)
    except Exception as exc:
        apath.write_text(before, encoding="utf-8")
        die(f"{action} would leave adapter.yaml invalid ({exc}), rolled back")
    for f in gen_project(target, core_dir):
        print(f"  regenerated: {f.relative_to(target)}")


def cmd_add_check(args) -> None:
    target = _stamped_target(args.dir)
    core_dir = _project_core(target)
    cfg = _load_project_cfg(target, core_dir)
    if any(c.get("id") == args.id for c in cfg.checks):
        die(f"check id {args.id!r} already exists in adapter.yaml. Pick a "
            f"new id or edit the existing entry by hand")
    if args.enforces and not _rule_annotated(target, cfg, args.enforces):
        # Accepting an orphaned enforces plants a rule-coverage failure at
        # the NEXT lint: F1's delayed-breakage pattern one layer up.
        die(f"`--enforces {args.enforces}` has no live "
            f"<!-- rule:{args.enforces} --> annotation in the project docs. "
            f"Annotate the rule first, or omit --enforces")
    apath = target / "harness" / "adapter" / "adapter.yaml"
    lines = apath.read_text(encoding="utf-8").splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^checks:", ln)),
               None)
    if idx is None:
        die("adapter.yaml has no top-level `checks:` key")
    # `checks: []` (the seed default) -> open the list, keeping any comment.
    lines[idx] = re.sub(r"^checks:\s*\[\]", "checks:", lines[idx])
    insert_at = idx + 1
    while insert_at < len(lines) and re.match(r"^\s+\S", lines[insert_at]):
        insert_at += 1  # append after existing entries
    entry = (f"  - {{id: {args.id}, cmd: {json.dumps(args.cmd)}, "
             f"'on': {json.dumps(args.on)}, severity: {args.severity}"
             + (f", enforces: {args.enforces}" if args.enforces else "")
             + "}\n")
    lines.insert(insert_at, entry)
    _rewrite_validate_regen(target, core_dir, "".join(lines), "add-check")
    print(f"gatebench add-check: {args.id!r} added and gen blocks regenerated.")


def cmd_set_gate_command(args) -> None:
    target = _stamped_target(args.dir)
    core_dir = _project_core(target)
    cfg = _load_project_cfg(target, core_dir)
    if args.gate not in cfg.gate_commands:
        die(f"unknown gate {args.gate!r}. This project's gates are: "
            f"{', '.join(cfg.gates)}")
    apath = target / "harness" / "adapter" / "adapter.yaml"
    lines = apath.read_text(encoding="utf-8").splitlines(keepends=True)
    gc_idx = next((i for i, ln in enumerate(lines)
                   if re.match(r"^\s+gate_commands:", ln)), None)
    if gc_idx is None:
        die("adapter.yaml has no workflow.gate_commands block")
    gc_indent = len(lines[gc_idx]) - len(lines[gc_idx].lstrip())
    edit_at = None
    for i in range(gc_idx + 1, len(lines)):
        ln = lines[i]
        if ln.strip() and (len(ln) - len(ln.lstrip())) <= gc_indent:
            break  # left the gate_commands block
        if re.match(rf"^\s+{re.escape(args.gate)}:\s", ln):
            edit_at = i
            break
    if edit_at is None:
        die(f"gate {args.gate!r} not found inside gate_commands")
    indent = re.match(r"^\s*", lines[edit_at]).group(0)
    lines[edit_at] = f"{indent}{args.gate}: {json.dumps(args.command)}\n"
    _rewrite_validate_regen(target, core_dir, "".join(lines),
                            "set-gate-command")
    print(f"gatebench set-gate-command: {args.gate!r} updated and gen blocks "
          f"regenerated.")


# ---------------------------------------------------------------- CLI

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="gatebench.py", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="stamp a new project")
    p_init.add_argument("dir")
    p_init.add_argument("--answers", help="YAML answers file (skips the questionnaire)")
    p_init.add_argument("--defaults", action="store_true",
                        help="skip the questionnaire and accept every default "
                             "(project name = target dir name)")
    p_init.add_argument("--retrofit", action="store_true",
                        help="stamp into an existing dirty repo: preserve every "
                             "existing seed-target file (.fhnew beside it) and "
                             "commit ONLY the files init creates")
    p_init.set_defaults(func=cmd_init)

    for name, func, help_ in (
            ("update", cmd_update, "sync pristine core files; report drift"),
            ("diff", cmd_diff, "report core drift only"),
            ("gen", cmd_gen, "regenerate <!-- fh:gen --> doc blocks")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("dir")
        sp.set_defaults(func=func)

    p_up = sub.add_parser("upstream",
                          help="promote a project core file into the template")
    p_up.add_argument("dir")
    p_up.add_argument("file")
    p_up.set_defaults(func=cmd_upstream)

    p_ac = sub.add_parser("add-check",
                          help="add an adapter check AND regenerate gen "
                               "blocks in one step (comment-preserving)")
    p_ac.add_argument("dir")
    p_ac.add_argument("--id", required=True)
    p_ac.add_argument("--cmd", required=True,
                      help="command; {files}/{slug} substituted by lint")
    p_ac.add_argument("--on", action="append", required=True, metavar="GLOB",
                      help="repo-relative glob the check fires on (repeatable)")
    p_ac.add_argument("--severity", choices=["fatal", "warn"], default="fatal")
    p_ac.add_argument("--enforces", help="rule-id this check enforces")
    p_ac.set_defaults(func=cmd_add_check)

    p_sg = sub.add_parser("set-gate-command",
                          help="rewrite one workflow.gate_commands entry AND "
                               "regenerate gen blocks in one step")
    p_sg.add_argument("dir")
    p_sg.add_argument("gate")
    p_sg.add_argument("command")
    p_sg.set_defaults(func=cmd_set_gate_command)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
