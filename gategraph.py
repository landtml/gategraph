#!/usr/bin/env python3
"""gategraph: GateGraph template stamper and sync tool.

Subcommands (plan §4):
  init <dir>      questionnaire + stamp core/ + seed/ + MANIFEST.json + git commit
  update <dir>    (P3) overwrite pristine core files; .fhnew + drift report otherwise
  diff <dir>      (P3) report core drift only
  upstream <dir> <file>   (P3) promote a project-modified core file to the template
  gen <dir>       (P3) regenerate <!-- {GEN_MARKER} --> derived doc blocks
  add-check <dir> --id X --cmd C --gate G [--on GLOB]   add a check to a gate
  set-gate-checks <dir> <gate> <check>...               set a gate's checks

stdlib + PyYAML only. Core files are stamped verbatim (hash-tracked, fh-owned);
seed files are rendered once at init and never touched again (adapter-owned).
"""
import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

TEMPLATE_ROOT = Path(__file__).resolve().parent
if str(TEMPLATE_ROOT) not in sys.path:
    sys.path.insert(0, str(TEMPLATE_ROOT))
# Names only (core/brand.py): the stamper never imports a kernel module.
from core.brand import GEN_MARKER  # noqa: E402
CORE_SRC = TEMPLATE_ROOT / "core"
SEED_SRC = TEMPLATE_ROOT / "seed"

SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
# B27: unit_dir is a relative path inside the project; no "..", no absolute path,
# and only characters that are safe inside the double-quoted protected_paths.
UNIT_DIR_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?$")
MANIFEST_REL = "harness/.fh/MANIFEST.json"
ANSWERS_REL = "harness/.fh/answers.yaml"
DEFAULT_GATES = ["composed", "checked", "verified"]
FIVE_GATE_PRESET = ["scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"]
ACCT_DENY = ("    - {id: acct, regex: '\\d{6}\\s*-?\\s*\\d{4}', scope: [any], severity: fatal, "
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
    "checks/policy_scan.py": "harness/adapter/checks/policy_scan.py",
    "dot_claude/settings.json": ".claude/settings.json",
    "dot_claude/agents/reviewer.md": ".claude/agents/reviewer.md",
}
GITIGNORE_SEED = "dot_gitignore"
# M2: the stamped core is core/ minus its packs and its tests, except selftest.py.
CORE_PACKS_DIR = "packs"
# The only test-tree files the stamp carries: the kernel self-test and the legacy
# `make check` shim (item 10). The rest of core/tests/ stays in the template.
CORE_TEST_KEEP = ("tests/selftest.py", "tests/run_checks.sh")
# Gates that take an approval (a human answers them) by default, when named in the gates.
APPROVAL_PRESET = ("scope_confirmed",)
APPROVAL_CHANNELS = ("tty", "token")
DEFAULT_CHANNEL = "tty"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def template_version() -> str:
    return (TEMPLATE_ROOT / "VERSION").read_text().strip()


def _version_key(v) -> "tuple | None":
    """Comparable key for X.Y.Z[-pre]; a pre-release sorts below its release.
    None when the string is not a version we can compare."""
    m = VERSION_RE.match(str(v).strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
            0 if m.group(4) else 1)


def die(msg: str, code: int = 1) -> "NoReturn":
    print(f"gategraph: error: {msg}", file=sys.stderr)
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
    if not SLUG_RE.match(out["artifact_noun"]):
        die(f"artifact_noun {out['artifact_noun']!r} must be a lowercase slug "
            f"(letters, digits, - and _, starting with a letter)")
    out["unit_dir"] = str(a.get("unit_dir") or "units").strip()
    if not UNIT_DIR_RE.match(out["unit_dir"]):
        die(f"unit_dir {out['unit_dir']!r} must be a relative path inside the "
            f"project (letters, digits, . _ - and / only; no ../ and no absolute path)")
    gates = a.get("gates") or DEFAULT_GATES
    if isinstance(gates, str) and gates.strip().lower() == "five-gate":
        gates = FIVE_GATE_PRESET
    out["gates"] = _parse_gate_list(gates, "gates")
    approvals = a.get("approval_gates")
    if approvals is None:
        approvals = [g for g in out["gates"] if g in APPROVAL_PRESET]
    out["approval_gates"] = _parse_gate_list(approvals, "approval_gates")
    unknown = set(out["approval_gates"]) - set(out["gates"])
    if unknown:
        die(f"approval_gates not in gates: {sorted(unknown)}")
    channel = str(a.get("approval_channel") or DEFAULT_CHANNEL).strip()
    if channel not in APPROVAL_CHANNELS:
        die(f"approval_channel {channel!r} must be one of {', '.join(APPROVAL_CHANNELS)}")
    out["approval_channel"] = channel
    packs = _parse_gate_list(a.get("packs") or [], "packs")  # same slug rule as gate names
    available = available_packs()
    for name in packs:
        if name not in available:
            die(f"packs: unknown pack {name!r} (available: {', '.join(available) or 'none'})")
    out["packs"] = packs
    out["seed_deny"] = bool(a.get("seed_deny", True))
    return out


def available_packs() -> list:
    """Pack names the template ships (core/packs/<name>/pack.yaml), sorted."""
    return sorted(p.parent.name for p in (CORE_SRC / CORE_PACKS_DIR).glob("*/pack.yaml"))


def questionnaire(target: Path) -> dict:
    """The ~7-prompt init questionnaire (§6). Everything else is edited in
    adapter.yaml later."""
    print("gategraph init: questionnaire (Enter accepts the [default])")

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
    chosen = (FIVE_GATE_PRESET if a["gates"].strip().lower() == "five-gate"
              else [g.strip() for g in a["gates"].split(",") if g.strip()])
    a["approval_gates"] = ask("which gates wait for a human approval (comma-separated, "
                              "empty for none)?",
                              ",".join(g for g in chosen if g in APPROVAL_PRESET))
    a["approval_channel"] = ask("approval channel: tty (answer from a terminal) or token "
                                "(one-time token)", DEFAULT_CHANNEL)
    a["packs"] = ask(f"domain packs to stamp ({', '.join(available_packs())}; empty for none)",
                     "")
    a["seed_deny"] = ask("seed the example deny-regex (account-number guard)? y/n", "y") \
        .lower().startswith("y")
    return a


# ---------------------------------------------------------------- rendering

def render_gates_block(answers: dict) -> str:
    """Schema-2 gates. Each non-approval gate runs the policy scan over its unit; an approval
    gate waits for a human on the adapter's channel. The last gate seals the unit."""
    approvals = set(answers["approval_gates"])
    last = len(answers["gates"]) - 1
    lines = []
    for index, gate in enumerate(answers["gates"]):
        if gate in approvals:
            work = f"approval: {{required: true, channel: {answers['approval_channel']}}}"
        else:
            work = "checks: [policy]"
        seal = ", seals_unit: true" if index == last else ""
        lines.append(f"  - {{id: {gate}, {work}{seal}}}")
    return "\n".join(lines)


def render_policy_block(answers: dict) -> str:
    deny = f"  deny:\n{ACCT_DENY}" if answers["seed_deny"] else "  deny: []"
    return f"{deny}\n  ask: []\n  blind: []"


def gate_chain(answers: dict) -> str:
    return " -> ".join(f"`{g}`" for g in answers["gates"])


def yaml_flow(items: list) -> str:
    return "[" + ", ".join(items) + "]"


def render(text: str, answers: dict) -> str:
    tokens = {
        "FH_PROJECT": answers["project"],
        "FH_ARTIFACT_NOUN": answers["artifact_noun"],
        "FH_UNIT_DIR": answers["unit_dir"],
        "FH_PACKS": yaml_flow(answers["packs"]),
        "FH_GATES_BLOCK": render_gates_block(answers),
        "FH_POLICY_BLOCK": render_policy_block(answers),
        "FH_GATE_CHAIN": gate_chain(answers),
        "FH_RULES_BLOCK": ACCT_RULE if answers["seed_deny"] else
            "(no project rules yet: add enforceable rules under `policy.deny` in\n"
            "`harness/adapter/adapter.yaml`)",
    }
    # B27: these reach adapter.yaml as YAML values; emit them double-quoted so `yes` or
    # `on` stays a string. Elsewhere (prose, protected_paths) they are raw.
    for key, name in (("project", "FH_PROJECT"), ("unit_noun", "FH_ARTIFACT_NOUN"),
                      ("unit_dir", "FH_UNIT_DIR")):
        pattern = re.compile(rf"^{key}: " + re.escape("{{" + name + "}}"), re.M)
        text = pattern.sub(lambda m, v=tokens[name]: f"{key}: {json.dumps(v)}", text)
    for name, value in tokens.items():
        text = text.replace("{{" + name + "}}", value)
    leftover = re.search(r"\{\{FH_[A-Z_]+\}\}", text)
    if leftover:
        die(f"unrendered template token {leftover.group(0)}")
    return text


# ---------------------------------------------------------------- core access
# Config and gen run inside the project, through the project's own stamped core (see the
# config-editing section). gategraph imports core.brand (names only), never a kernel module.

def gen_project(target: Path) -> list:
    """Regenerate the gen blocks of a stamped project; returns the changed files (relative)."""
    return _regenerate(target)


# ---------------------------------------------------------------- stamping

def write_rendered(src: Path, dst: Path, answers: dict) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(render(src.read_text(), answers))
    shutil.copymode(src, dst)


def core_files() -> "list[Path]":
    """The template files that make up the stamped core (M2): every file under core/ except
    __pycache__, the packs (stamped per selection by stamp_packs), and the test suite, whose
    only member that is stamped is tests/selftest.py."""
    keep = []
    for src in sorted(CORE_SRC.rglob("*")):
        if not src.is_file() or "__pycache__" in src.parts or src.suffix == ".pyc":
            continue
        rel = src.relative_to(CORE_SRC).as_posix()
        if rel.split("/", 1)[0] == CORE_PACKS_DIR:
            continue
        if rel.startswith("tests/") and rel not in CORE_TEST_KEEP:
            continue
        keep.append(src)
    return keep


def stamp_core(target: Path) -> dict:
    """Copy the stamped core into harness/core/; return {project-relative path: sha256}."""
    hashes = {}
    for src in core_files():
        rel = src.relative_to(CORE_SRC)
        dst = target / "harness" / "core" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        hashes[(Path("harness") / "core" / rel).as_posix()] = sha256_file(dst)
    return hashes


def stamp_packs(target: Path, answers: dict) -> "tuple[dict, list[str]]":
    """Copy each selected pack to packs/<name>/ (the loader and the pack commands name
    that path). Returns ({pack: {pack-relative path: sha256}}, project-relative paths)."""
    hashes, rels = {}, []
    for name in answers["packs"]:
        src_root = CORE_SRC / CORE_PACKS_DIR / name
        files = {}
        for src in sorted(src_root.rglob("*")):
            rel = src.relative_to(src_root)
            if (not src.is_file() or "__pycache__" in rel.parts or src.suffix == ".pyc"
                    or rel.parts[0] == "examples"):
                continue
            dst = target / "packs" / name / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            files[rel.as_posix()] = sha256_file(dst)
            rels.append((Path("packs") / name / rel).as_posix())
        hashes[name] = files
    return hashes, rels


def seed_targets(answers: dict) -> "list[str]":
    """Project-relative paths the seed stamp would write (conflict surface)."""
    # .gitignore is merge-not-overwrite, so it is never a conflict.
    return [dst.format(project=answers["project"]) for dst in SEED_MAP.values()]


def stamp_seed(root: Path, answers: dict, retrofit: bool = False,
               live: "Path | None" = None) -> "tuple[list[str], list[str]]":
    """Render the seed into ``root``. Returns (written, preserved) project-relative
    paths: ``written`` is what init created or merged, the only paths the stamp
    commit may stage.

    ``live`` is the real target. In ``retrofit`` mode an existing destination
    there is NEVER overwritten: the rendered template lands beside it as
    ``<file>.fhnew`` (the fh-update convention) and the file is reported as
    preserved."""
    live = live or root
    written: "list[str]" = []
    preserved: "list[str]" = []

    def place(src: Path, rel: str) -> None:
        dst = root / rel
        if retrofit and (live / rel).exists():
            write_rendered(src, dst.with_name(dst.name + ".fhnew"), answers)
            preserved.append(rel)
        else:
            write_rendered(src, dst, answers)
            written.append(rel)

    for src_rel, dst_rel in SEED_MAP.items():
        place(SEED_SRC / src_rel, dst_rel.format(project=answers["project"]))
    # .gitignore: create, or append our lines to an existing one (retrofit case).
    # Written (and so staged) only when it actually changes.
    seed_lines = (SEED_SRC / GITIGNORE_SEED).read_text()
    gitignore = live / ".gitignore"
    if gitignore.exists():
        have = gitignore.read_text()
        missing = [l for l in seed_lines.splitlines() if l and l not in have.splitlines()]
        if missing:
            (root / ".gitignore").write_text(
                have.rstrip("\n") + "\n" + "\n".join(missing) + "\n")
            written.append(".gitignore")
    else:
        (root / ".gitignore").write_text(seed_lines)
        written.append(".gitignore")
    return written, preserved


def write_manifest(target: Path, core_hashes: dict, pack_hashes: "dict | None" = None) -> None:
    """MANIFEST.json: ``files`` is the core (the key `gategraph update` reads, unchanged);
    ``packs`` holds each selected pack as its own map, so a pack is tracked separately."""
    fh_dir = target / "harness" / ".fh"
    fh_dir.mkdir(parents=True, exist_ok=True)
    packs = {name: {"files": files} for name, files in (pack_hashes or {}).items()}
    manifest = {"version": template_version(), "files": core_hashes, "packs": packs}
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
           f"gategraph init: stamp GateGraph v{template_version()} ({project})"]
    if only is not None:
        cmd += ["--", *only]  # pathspec commit: user's staged files stay staged
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def prove_stamp_alive(target: Path) -> None:
    if os.environ.get("FH_SKIP_STAMP_SELFCHECK") == "1":
        # F10: test rigs that stamp many projects assert core integrity
        # separately (gategraph diff); real inits keep the self-check default-ON.
        print("  stamp self-check: SKIPPED (FH_SKIP_STAMP_SELFCHECK=1)")
        return
    # The kernel selftest needs only the standard library and PyYAML, so init does not need
    # the project venv (`make venv`) to prove the stamp alive.
    proc = subprocess.run([sys.executable, "harness/core/tests/selftest.py"],
                          cwd=target, capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"stamp self-check failed:\n{proc.stdout}\n{proc.stderr}")
    print(f"  stamp self-check: GREEN (harness/core/tests/selftest.py: "
          f"{proc.stdout.strip() or 'ok'})")


def _validate_stage(stage: Path) -> None:
    """Load the rendered adapter with the schema-2 loader (kernel, standard library and
    PyYAML) in a child interpreter, so the stage's own core is the one that judges it."""
    code = ("import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from core.kernel import config\n"
            "try:\n"
            "    config.load(sys.argv[2])\n"
            "except config.ConfigError as exc:\n"
            "    print('\\n'.join(exc.problems))\n"
            "    sys.exit(3)\n")
    proc = subprocess.run([sys.executable, "-c", code, str(stage / "harness"), str(stage)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        die(f"the rendered adapter.yaml is invalid:\n{proc.stdout}{proc.stderr}"
            f"Nothing was written.")


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


def _move_into_place(stage: Path, target: Path) -> None:
    """Move a fully rendered stage into the target. Every clash is checked
    before the first move; the manifest moves last, so an interrupted move never
    looks like a finished stamp. __pycache__ is never stamped."""
    def ours(p: Path) -> bool:
        return "__pycache__" not in p.relative_to(stage).parts

    dirs = sorted(p for p in stage.rglob("*") if p.is_dir() and ours(p))
    files = sorted(p for p in stage.rglob("*") if p.is_file() and ours(p))
    rels = [f.relative_to(stage).as_posix() for f in files]
    clash = [r for r in rels if (target / r).exists()
             and r != ".gitignore" and not r.endswith(".fhnew")]
    if clash:
        die("these files already exist and init would overwrite them (nothing "
            "was written):\n" + "".join(f"    {r}\n" for r in clash))
    target.mkdir(parents=True, exist_ok=True)
    for d in dirs:
        (target / d.relative_to(stage)).mkdir(parents=True, exist_ok=True)
    for r in rels:
        if r == MANIFEST_REL:
            continue
        (target / r).parent.mkdir(parents=True, exist_ok=True)
        os.replace(stage / r, target / r)
    os.replace(stage / MANIFEST_REL, target / MANIFEST_REL)


def cmd_init(args) -> None:
    target = Path(args.dir).resolve()
    if (target / "harness" / ".fh" / "MANIFEST.json").exists():
        die(f"{target} is already stamped (harness/.fh/MANIFEST.json exists); "
            f"use `gategraph update` to sync it")
    if args.answers:
        raw = yaml.safe_load(Path(args.answers).read_text()) or {}
        answers = normalize_answers(raw, target)
    elif getattr(args, "defaults", False):
        answers = normalize_answers({}, target)  # every questionnaire default
    else:
        answers = normalize_answers(questionnaire(target), target)
    _preflight_gitignore(target)
    retrofit = bool(getattr(args, "retrofit", False))

    # F11: refuse BEFORE writing anything when the target already has files
    # the seed would overwrite (the P6 retrofit nearly lost a live HANDOFF.md).
    conflicts = [rel for rel in seed_targets(answers) if (target / rel).exists()]
    if conflicts and not retrofit:
        die("these files already exist and gategraph init would OVERWRITE them:\n"
            + "".join(f"    {r}\n" for r in conflicts)
            + "  re-run with --retrofit to preserve them (template versions land "
            "beside each as .fhnew,\n  and the stamp commit stages ONLY what "
            "init creates; your dirty tree stays yours)")

    # B28: the whole stamp is rendered, validated and gen'd in a staging directory
    # beside the target. Only a stamp that passed every check is moved in.
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.fh-stage-",
                                  dir=target.parent)).resolve()
    try:
        core_hashes = stamp_core(stage)
        pack_hashes, pack_rels = stamp_packs(stage, answers)
        written, preserved = stamp_seed(stage, answers, retrofit=retrofit, live=target)
        write_manifest(stage, core_hashes, pack_hashes)
        (stage / ANSWERS_REL).write_text(yaml.safe_dump(answers, sort_keys=True))
        _validate_stage(stage)
        _move_into_place(stage, target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    for rel in preserved:
        print(f"  preserved: {rel} (yours untouched; template version at {rel}.fhnew)")
    if retrofit:
        # B15: stage exactly the paths init wrote, never a whole directory
        only = sorted(set(written + list(core_hashes) + pack_rels
                          + [MANIFEST_REL, ANSWERS_REL]))
        git_commit_stamp(target, answers["project"], only=only)
    else:
        git_commit_stamp(target, answers["project"])
    prove_stamp_alive(target)
    print(f"gategraph init: stamped {answers['project']} at {target} "
          f"(template v{template_version()}, {len(core_hashes)} core files)")


# ---------------------------------------------------------------- sync

def _template_core_files() -> dict:
    """{relpath-under-core: absolute source Path} for exactly the files init stamps (the
    same list as core_files()): no tests except selftest.py, no packs (B-M2)."""
    return {src.relative_to(CORE_SRC).as_posix(): src for src in core_files()}


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
    stamped = manifest.get("version")
    now = template_version()
    old_key, new_key = _version_key(stamped), _version_key(now)
    if old_key is None or new_key is None:
        print(f"gategraph update: warning: cannot compare template v{now} with "
              f"stamped v{stamped}; downgrade check skipped")
    elif new_key < old_key:
        if not getattr(args, "allow_downgrade", False):
            die(f"refusing to downgrade {target.name}: it is stamped at v{stamped} "
                f"and this template is v{now}. Older core files would replace newer "
                f"ones. Update the template, or re-run with --allow-downgrade")
        print(f"gategraph update: --allow-downgrade: v{stamped} -> v{now}")
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
        print(f"\ngategraph update: DRIFT: {len(drifted)} locally modified file(s) "
              f"NOT overwritten (template version saved as .fhnew):")
        for k in drifted:
            print(f"  ! {k}")
        print("  resolve each: merge the .fhnew by hand, or promote your edit "
              "with `gategraph upstream`.")
    print(f"gategraph update: {target.name} -> v{template_version()} "
          f"({len(updated)} updated, {len(created)} new, {len(removed)} removed, "
          f"{len(drifted)} drifted)")


def cmd_diff(args) -> None:
    """Report drift only, no writes. Covers BOTH directions: manifest files
    that drifted locally AND template files the manifest has never seen
    (upstream additions `gategraph update` would stamp)."""
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
        print(f"  modified: {k}  (promote: gategraph upstream, or restore: gategraph update)")
    for k in missing:
        print(f"  missing:  {k}  (restore: gategraph update)")
    for k in outdated:
        print(f"  outdated: {k}  (template has a newer version: gategraph update)")
    for k in new_upstream:
        print(f"  new upstream: {k}  (gategraph update will add it)")
    if not (modified or missing or outdated or new_upstream):
        print("gategraph diff: core is pristine and current.")
    if modified or missing:
        sys.exit(1)


def cmd_upstream(args) -> None:
    """Promote a project-modified core file back into the template repo:
    how a harness improvement grown in one project flows to all of them.

    Refuses when the template has moved on since this project stamped the file
    (the manifest hash must equal the template's current hash), so newer
    template work is never overwritten. The path must stay inside core/."""
    target = _stamped_target(args.dir)
    manifest = _read_manifest(target)
    rel = args.file
    for prefix in ("harness/core/", "core/"):
        rel = rel.removeprefix(prefix)
    rel_path = Path(rel)
    if not rel or rel_path.is_absolute() or ".." in rel_path.parts:
        die(f"upstream: {args.file!r} is not a path inside core/")
    core_dir = target / "harness" / "core"
    src = core_dir / rel_path
    dst = CORE_SRC / rel_path
    if not src.resolve().is_relative_to(core_dir.resolve()) \
            or not dst.resolve().is_relative_to(CORE_SRC.resolve()):
        die(f"upstream: {args.file!r} resolves outside core/")
    if not src.is_file():
        die(f"no such core file in project: harness/core/{rel}")

    key = f"harness/core/{rel}"
    recorded = manifest["files"].get(key)
    if dst.is_file():
        now = sha256_file(dst)
        if recorded != now:
            diff = difflib.unified_diff(
                dst.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
                src.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True),
                fromfile=f"template/core/{rel}", tofile=f"{target.name}/{key}")
            sys.stdout.writelines(diff)
            die(f"refusing upstream of {rel}: the template has moved on since this "
                f"project stamped it (stamped {str(recorded)[:12]}, template now "
                f"{now[:12]}). The diff above is template -> project. Merge the newer "
                f"template work into your copy first, then re-run")
    elif recorded is not None:
        die(f"refusing upstream of {rel}: the template removed it after this "
            f"project stamped it")

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    # the project copy now matches the template again: re-record it
    manifest["files"][key] = sha256_file(src)
    _manifest_path(target).write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"gategraph upstream: core/{rel} promoted to the template.\n"
          f"  next: run the template test suite, bump VERSION, note it in "
          f"CHANGELOG.md, then `gategraph update` your other projects.")


def cmd_gen(args) -> None:
    target = _stamped_target(args.dir)
    _stamped_core(target)
    changed = gen_project(target)
    for rel in changed:
        print(f"  regenerated: {rel}")
    print(f"gategraph gen: {len(changed)} file(s) regenerated.")


def cmd_migrate(args) -> None:
    """Plan 4.9: the migration lives in core/cli/migrate.py; this only wires the subcommand."""
    if str(TEMPLATE_ROOT) not in sys.path:
        sys.path.insert(0, str(TEMPLATE_ROOT))
    from core.cli import migrate
    code, plan = migrate.run(args.dir, apply=args.apply, allow_dirty=args.allow_dirty)
    print("\n".join(plan.lines))
    sys.exit(code)


# ------------------------------------------------- config editing (F1+F8, schema 2)
# adapter.yaml is the single source of truth AND its own documentation, so programmatic edits
# are targeted TEXT operations (yaml.safe_dump round-trips destroy every comment, F8). Every
# edit is validated by the project's OWN loader (harness/core/kernel/config.py, schema 2) and
# rolled back when that loader refuses it; then the gen blocks are regenerated in the same step
# (F1). Loader and generator run as subprocesses with PYTHONPATH=<project>/harness, so each
# project is judged by its own stamped core version.
#
# The gate command is generated (plan 1.7: `gate attempt <gate> --unit <slug>`, or `proceed` for
# an approval gate), so nothing here writes command text: the printed next command is derived.

GATE_CMD = ".venv/bin/python harness/core/cli/gate.py"
# Prints one JSON object: the gates (with their checks and whether they take an approval) and
# the check ids, or the loader's problems. Runs inside the project, against its own core.
LOAD_SNIPPET = (
    "import json\n"
    "from pathlib import Path\n"
    "from core.kernel import config\n"
    "try:\n"
    "    cfg = config.load(Path('.').resolve())\n"
    "except config.ConfigError as exc:\n"
    "    print(json.dumps({'ok': False, 'problems': list(exc.problems)}))\n"
    "else:\n"
    "    print(json.dumps({'ok': True,\n"
    "        'gates': {g.id: {'checks': list(g.checks), 'approval': g.approval is not None}\n"
    "                  for g in cfg.gates},\n"
    "        'checks': [c.id for c in cfg.checks]}))\n"
)


def _stamped_core(target: Path) -> None:
    if not (target / "harness" / "core" / "kernel" / "config.py").is_file():
        die(f"{target} has no harness/core/kernel (not a stamped project)")


def _adapter_path(target: Path) -> Path:
    return target / "harness" / "adapter" / "adapter.yaml"


def _project_run(target: Path, *argv: str) -> subprocess.CompletedProcess:
    """Run a python command inside the project with its own stamped core on the path."""
    env = {**os.environ, "PYTHONPATH": str(target / "harness")}
    return subprocess.run([sys.executable, *argv], cwd=target, env=env,
                          capture_output=True, text=True)


def _try_load(target: Path) -> tuple:
    """(state, []) when the project's loader accepts adapter.yaml, else (None, problems)."""
    proc = _project_run(target, "-c", LOAD_SNIPPET)
    lines = proc.stdout.strip().splitlines()
    if proc.returncode != 0 or not lines:
        die(f"cannot run the project's own loader: {(proc.stderr or proc.stdout).strip()}")
    data = json.loads(lines[-1])
    if data["ok"]:
        return data, []
    return None, data["problems"]


def _current(target: Path) -> dict:
    state, problems = _try_load(target)
    if problems:
        die("adapter.yaml does not load; fix it by hand before editing:\n"
            + "".join(f"  - {p}\n" for p in problems).rstrip("\n"))
    return state


def _regenerate(target: Path) -> list:
    """Regenerate the gen blocks with the project's own kernel; returns the files changed."""
    proc = _project_run(target, "-m", "core.kernel.gen", "--root", str(target))
    if proc.returncode != 0:
        die(f"gen refused: {(proc.stderr or proc.stdout).strip()}. Repair the gen markers, "
            f"then run from the project root: PYTHONPATH=harness python3 -m core.kernel.gen")
    return [ln.split("regenerated:", 1)[1].strip()
            for ln in proc.stdout.splitlines() if ln.startswith("  regenerated:")]


def _commit(target: Path, new_text: str, action: str) -> dict:
    """Write adapter.yaml, validate it with the project's loader (roll back on refusal), then
    regenerate the gen blocks in the same step. Returns the loaded state."""
    apath = _adapter_path(target)
    before = apath.read_text(encoding="utf-8")
    apath.write_text(new_text, encoding="utf-8")
    state, problems = _try_load(target)
    if problems:
        apath.write_text(before, encoding="utf-8")
        die(f"{action} would leave adapter.yaml invalid, rolled back:\n"
            + "".join(f"  - {p}\n" for p in problems).rstrip("\n"))
    for rel in _regenerate(target):
        print(f"  regenerated: {rel}")
    return state


def _gate_line(lines: list, gate: str) -> int:
    """The index of the one-line flow entry `- {id: <gate>, ...}` in gates:."""
    pattern = re.compile(rf"^\s*-\s*\{{\s*id:\s*{re.escape(gate)}\s*[,}}]")
    hits = [i for i, ln in enumerate(lines) if pattern.match(ln)]
    if len(hits) != 1:
        die(f"gate {gate!r}: expected one one-line `- {{id: {gate}, ...}}` entry in gates:, "
            f"found {len(hits)}. Edit that entry in adapter.yaml by hand")
    return hits[0]


def _set_gate_checks_text(text: str, gate: str, checks: list) -> str:
    """adapter.yaml text with the gate's checks list replaced (or added)."""
    lines = text.splitlines(keepends=True)
    idx = _gate_line(lines, gate)
    body = lines[idx].rstrip("\r\n")
    newline = lines[idx][len(body):]
    listing = "checks: " + yaml_flow(list(checks))
    if re.search(r"checks:\s*\[[^\]]*\]", body):
        body = re.sub(r"checks:\s*\[[^\]]*\]", lambda _m: listing, body, count=1)
    else:
        close = body.rfind("}")
        if close < 0:
            die(f"gate {gate!r}: cannot place checks in its entry; edit adapter.yaml by hand")
        body = body[:close].rstrip() + ", " + listing + body[close:]
    lines[idx] = body + newline
    return "".join(lines)


def _add_check_text(text: str, cid: str, command: str, on: list, severity: str) -> str:
    """adapter.yaml text with one entry appended to the top-level checks: list."""
    lines = text.splitlines(keepends=True)
    idx = next((i for i, ln in enumerate(lines) if re.match(r"^checks:", ln)), None)
    if idx is None:
        die("adapter.yaml has no top-level `checks:` key")
    # `checks: []` -> open the list, keeping any comment on the line.
    lines[idx] = re.sub(r"^checks:\s*\[\]", "checks:", lines[idx])
    insert_at = idx + 1
    while insert_at < len(lines) and re.match(r"^\s+\S", lines[insert_at]):
        insert_at += 1  # after the existing entries and their comments
    fields = [f"id: {cid}", f"command: {json.dumps(command)}", f"severity: {severity}"]
    if on:
        fields.append(f"'on': {json.dumps(list(on))}")
    lines.insert(insert_at, "  - {" + ", ".join(fields) + "}\n")
    return "".join(lines)


def _print_next(gate: str, state: dict) -> None:
    if state["gates"][gate]["approval"]:
        cmd = f"{GATE_CMD} proceed --unit <slug> --decision approve --by <name>"
    else:
        cmd = f"{GATE_CMD} attempt {gate} --unit <slug>"
    print(f"  gate command: {cmd}")


def _unknown_gate(gate: str, state: dict) -> None:
    die(f"unknown gate {gate!r}. This project's gates are: {', '.join(state['gates'])}")


def cmd_add_check(args) -> None:
    target = _stamped_target(args.dir)
    _stamped_core(target)
    state = _current(target)
    if args.id in state["checks"]:
        die(f"check id {args.id!r} already exists in adapter.yaml. Pick a new id or edit "
            f"the existing entry by hand")
    if args.gate not in state["gates"]:
        _unknown_gate(args.gate, state)
    text = _add_check_text(_adapter_path(target).read_text(encoding="utf-8"), args.id,
                           args.cmd, args.on or [], args.severity)
    checks = list(state["gates"][args.gate]["checks"]) + [args.id]
    text = _set_gate_checks_text(text, args.gate, checks)
    new_state = _commit(target, text, "add-check")
    print(f"gategraph add-check: {args.id!r} added to gate {args.gate!r} and gen blocks "
          f"regenerated.")
    _print_next(args.gate, new_state)


def cmd_set_gate_checks(args) -> None:
    target = _stamped_target(args.dir)
    _stamped_core(target)
    state = _current(target)
    if args.gate not in state["gates"]:
        _unknown_gate(args.gate, state)
    unknown = [c for c in args.checks if c not in state["checks"]]
    if unknown:
        die(f"unknown check(s) {', '.join(unknown)}. This project's checks are: "
            f"{', '.join(state['checks'])}")
    text = _set_gate_checks_text(_adapter_path(target).read_text(encoding="utf-8"),
                                 args.gate, args.checks)
    new_state = _commit(target, text, "set-gate-checks")
    print(f"gategraph set-gate-checks: gate {args.gate!r} now runs "
          f"{', '.join(args.checks)}; gen blocks regenerated.")
    _print_next(args.gate, new_state)


# ---------------------------------------------------------------- CLI

def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="gategraph.py",
                                     description=__doc__.replace("{GEN_MARKER}", GEN_MARKER))
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
            ("gen", cmd_gen, f"regenerate <!-- {GEN_MARKER} --> doc blocks")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("dir")
        sp.set_defaults(func=func)

    p_upd = sub.choices["update"]
    p_upd.add_argument("--allow-downgrade", action="store_true",
                       help="permit a template older than the stamped version "
                            "(overwrites newer core files with older ones)")

    p_up = sub.add_parser("upstream",
                          help="promote a project core file into the template")
    p_up.add_argument("dir")
    p_up.add_argument("file")
    p_up.set_defaults(func=cmd_upstream)

    p_ac = sub.add_parser("add-check",
                          help="add an adapter check to one gate AND regenerate gen "
                               "blocks in one step (comment-preserving)")
    p_ac.add_argument("dir")
    p_ac.add_argument("--id", required=True)
    p_ac.add_argument("--cmd", required=True,
                      help="command the check runs; {unit} is substituted by the runner")
    p_ac.add_argument("--on", action="append", default=None, metavar="GLOB",
                      help="repo-relative glob the check reads (repeatable)")
    p_ac.add_argument("--severity", choices=["fatal", "warn"], default="fatal")
    p_ac.add_argument("--gate", required=True, help="the gate that runs this check")
    p_ac.set_defaults(func=cmd_add_check)

    p_mig = sub.add_parser("migrate",
                           help="move a stamped version-1 project to the schema-2 layout "
                                "(dry run unless --apply)")
    p_mig.add_argument("dir")
    p_mig.add_argument("--apply", action="store_true",
                       help="perform the steps and make one commit")
    p_mig.add_argument("--allow-dirty", action="store_true",
                       help="permit a dirty tree (the commit holds only the migration's paths)")
    p_mig.set_defaults(func=cmd_migrate)

    p_sg = sub.add_parser("set-gate-checks",
                          help="set the checks one gate runs AND regenerate gen blocks "
                               "in one step")
    p_sg.add_argument("dir")
    p_sg.add_argument("gate")
    p_sg.add_argument("checks", nargs="+", metavar="CHECK")
    p_sg.set_defaults(func=cmd_set_gate_checks)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
