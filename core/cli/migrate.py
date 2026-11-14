"""gategraph migrate <dir>: move a stamped version-1 project to the schema-2 layout (plan 4.9; brief M4).

Dry run is the default. ``--apply`` performs the steps and makes one commit that holds only the
paths the migration wrote. Every write goes through a journal that keeps the bytes each path had
before its first write. Any failure, and any difference in effective gates at step 8, puts the
journal back and restores the state directories from the backup taken at step 1.

Steps (plan 4.9): 1 preflight, snapshot and backup; 2 core drift (kept as ``<name>.local``);
3 adapter (schema 1 translated by ``config_v1``, written as schema 2, the original kept as
adapter.v1.yaml); 4 core (the new core replaces harness/core, the hash manifest is rewritten,
hook registrations point at the host shims, the seed ignore rules are merged in); 5 environment
(the project venv from the lock file, or a stop with the tree consistent); 6 units (each unit's
thread is created through the graph, carrying one ``import`` record per gate that legacy status
showed as passed); 7 generated docs and the Makefile (the Makefile is never edited, and
``Makefile.v2`` is written beside it only when its ``check`` no longer runs); 8 verify (gate verify
per unit, effective gates against the step-1 snapshot, the project's ``make check``).

This module imports only the standard library, PyYAML and the kernel. Graph steps run in a child
interpreter: the project's venv, or the template's locked venv while the project has none.

The product name is not written here; see core/brand.py.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from core.kernel import config, config_v1, evidence, paths, surface

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = "harness/adapter/adapter.yaml"
ADAPTER_V1 = "harness/adapter/adapter.v1.yaml"
CORE = "harness/core"
LOCK = "harness/core/requirements.lock"
STAMP = "harness/.fh/MANIFEST.json"
ANSWERS = "harness/.fh/answers.yaml"
LEGACY_STATE = "harness/state"
NEW_STATE = ".gategraph"              # brand.STATE_DIRNAME: the runner's state directory
SNAPSHOT = f"{NEW_STATE}/migrate/snapshot.json"
MAKEFILE = "Makefile"
MAKEFILE_V2 = "Makefile.v2"
SETTINGS = ".claude/settings.json"
GITIGNORE = ".gitignore"
ENV_STAMP = ".venv/.lock-installed"
HOOK_OLD = "harness/core/hooks/"
HOOK_NEW = "harness/core/hosts/shims/"
# Only migration writes import records (evidence.IMPORT_ACTOR_ID; the guard is tested).
IMPORT_ACTOR = {"kind": "tool", "id": evidence.IMPORT_ACTOR_ID, "channel": "legacy"}
SEAM_TEXT = ("the host seam: one class and one HOSTS entry in core/hosts/bridge.py, plus the "
             "key in the shim (core/hosts/shims/). A second host is selected there, not by "
             "editing a hook file")
SEAMS = {"hooks/_common.py": SEAM_TEXT}
# Schema-1 keys with no schema-2 field (config_v1 docstring). Dropped keys are reported.
CONSUMED_V1 = {"fh_schema", "project", "artifact_noun", "unit_dir", "workflow", "policies",
               "protected_paths", "checks", "reviewer"}
CONSUMED_WORKFLOW = {"gates", "gate_commands", "hashed_gates", "tool_set_gates"}
DENY_KEYS = {"id", "regex", "scope", "severity", "summary", "enforces", "pretool"}
ASK_KEYS = {"paths", "reason"}
# legacy manifest status -> new thread status. A paused unit is in progress in the graph and
# is projected as paused (see CHILD).
STATUS_MAP = {"in_progress": "in_progress", "paused": "in_progress",
              "escalated": "escalated", "abandoned": "abandoned"}
# Runs inside the project, against its own core: one thread per unit, through the runner.
CHILD = r'''
import json, sys
from pathlib import Path
from core.kernel import config
from core.graph.unit import initial_state
from core.cli import gate as cli
root = Path(sys.argv[1]).resolve()
work = json.load(sys.stdin)
cfg = config.load(root)
out = {}
for item in work:
    ctx = cli.Ctx(root=root, cfg=cfg, unit=item["unit"], session_id=None)
    graph = cli._graph(ctx)
    if cli._snapshot(graph, ctx.thread).values:
        out[item["unit"]] = "already exists"
        continue
    payload = initial_state(project=cfg.project, unit=item["unit"],
                            config_digest=config.config_digest(cfg))
    payload["evidence"] = item["evidence"]
    payload["status"] = item["status"]
    cli._run(ctx, graph, payload)
    if item["paused"]:  # the legacy unit was paused: projected as paused, as `gate pause` does
        cli._project(ctx, cli._snapshot(graph, ctx.thread), status="paused")
    out[item["unit"]] = "created"
print(json.dumps(out))
'''


class MigrateError(Exception):
    """The migration cannot proceed. The message says what to do next."""


class StopHere(Exception):
    """Steps 1 to 4 are done and consistent; the environment (step 5) is missing."""


# ---------------------------------------------------------------- small helpers

_GB = None


def _gategraph():
    """The stamping module (core lists, template version), loaded from the template root."""
    global _GB
    if _GB is None:
        spec = importlib.util.spec_from_file_location("_gategraph_for_migrate",
                                                      TEMPLATE_ROOT / "gategraph.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GB = module
    return _GB


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(cmd, cwd, env=None, stdin=None):
    return subprocess.run([str(c) for c in cmd], cwd=str(cwd), capture_output=True, text=True,
                          input=stdin, env=env)


def _graph_env(project: Path) -> dict:
    return {**os.environ, "PYTHONPATH": str(project / "harness")}


def _iso(at: Any) -> str:
    """A legacy timestamp (naive local time) as ISO 8601 with a UTC offset."""
    try:
        when = dt.datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        raise MigrateError(f"legacy timestamp {at!r} is not ISO 8601") from None
    if when.tzinfo is None:
        when = when.astimezone()  # legacy records are naive local time
    return when.isoformat()


class Journal:
    """Every write and removal under the project, with the bytes and mode each path had first."""

    def __init__(self, project: Path):
        self.project = project
        self.before: dict[str, tuple[bytes, int] | None] = {}

    def note(self, rel: str) -> None:
        if rel not in self.before:
            path = self.project / rel
            self.before[rel] = ((path.read_bytes(), path.stat().st_mode & 0o7777)
                                if path.is_file() else None)

    def write(self, rel: str, data: bytes, mode_from: Path | None = None) -> None:
        self.note(rel)
        path = self.project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if mode_from is not None:
            shutil.copystat(mode_from, path)  # mode and mtime: make must not see a newer lock

    def text(self, rel: str, text: str) -> None:
        self.write(rel, text.encode("utf-8"))

    def remove(self, rel: str) -> None:
        self.note(rel)
        (self.project / rel).unlink(missing_ok=True)

    def paths(self) -> list[str]:
        return sorted(self.before)

    def rollback(self) -> None:
        for rel, saved in self.before.items():
            path = self.project / rel
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                data, mode = saved
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                os.chmod(path, mode)
        for rel in self.before:  # prune directories this run left empty
            directory = (self.project / rel).parent
            while (directory != self.project and directory.is_dir()
                   and not any(directory.iterdir())):
                directory.rmdir()
                directory = directory.parent


class Plan:
    """What the run did, or would do. Printed at the end."""

    def __init__(self):
        self.lines: list[str] = []

    def say(self, text: str) -> None:
        self.lines.append(text)

    def section(self, title: str) -> None:
        self.lines.append("")
        self.lines.append(f"== {title}")


# ---------------------------------------------------------------- step 1: preflight

def _version_key(gb, text):
    return gb._version_key(str(text))


def _preflight(project: Path, allow_dirty: bool, gb, plan: Plan) -> dict:
    if not (project / STAMP).is_file():
        raise MigrateError(f"{project} is not a stamped project (no {STAMP})")
    stamp = json.loads((project / STAMP).read_text())
    stamped, template = stamp.get("version"), gb.template_version()
    if _version_key(gb, stamped) is None or _version_key(gb, template) is None:
        raise MigrateError(f"cannot compare stamped v{stamped} with template v{template}")
    if _version_key(gb, stamped) > _version_key(gb, template):
        raise MigrateError(f"refusing: the project is stamped v{stamped}, newer than this "
                           f"template v{template}")
    porcelain = _run(["git", "-C", project, "status", "--porcelain"], cwd=project).stdout
    if porcelain.strip() and not allow_dirty:
        raise MigrateError("the working tree is dirty. Commit or stash first, or re-run with "
                           "--allow-dirty (the commit then holds only the migration's paths):\n"
                           + porcelain.rstrip())
    plan.say(f"step 1: stamped v{stamped}, template v{template}; tree "
             f"{'dirty (allowed by --allow-dirty)' if porcelain.strip() else 'clean'}")
    return stamp


def _legacy_units(project: Path, unit_dir: str) -> tuple[dict, list[str]]:
    """({slug: manifest path} of the version-1 units, [archived unit paths])."""
    root = project / unit_dir
    units: dict[str, Path] = {}
    archived: list[str] = []
    if not root.is_dir():
        return units, archived
    for manifest in sorted(root.rglob("manifest.yaml")):
        rel = manifest.parent.relative_to(root)
        if any(part.lower() == "archive" for part in rel.parts):
            archived.append(f"{unit_dir}/{rel.as_posix()}")
            continue
        if len(rel.parts) != 1:
            continue
        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and "schema" not in data and "slug" in data:
            units[rel.as_posix()] = manifest
    return units, archived


def _legacy_status(project: Path, slug: str) -> dict:
    """The legacy `gate.py status` of one unit: its status and the gates shown as passed."""
    done = _run([sys.executable, "harness/core/gate.py", "status", "--unit", slug], cwd=project)
    status, passed = None, None
    for line in done.stdout.splitlines():
        text = line.strip()
        if text.startswith("status:"):
            status = text.split(":", 1)[1].strip()
        elif text.startswith("gates passed:"):
            names = text.split(":", 1)[1].strip()
            passed = [] if names == "(none)" else [n.strip() for n in names.split(",")]
    if done.returncode != 0 or status is None or passed is None:
        raise MigrateError(f"legacy `gate.py status` of {slug} did not report a status:\n"
                           f"{done.stdout}{done.stderr}")
    return {"status": status, "passed": passed}


def _snapshot(project: Path, unit_dir: str, plan: Plan, proceeded: dict | None) -> tuple[dict, dict, list[str]]:
    """(snapshot of every unit, version-1 units, archived paths). A proceeded run reads the
    snapshot the first run wrote, because the legacy command is gone by then."""
    units, archived = _legacy_units(project, unit_dir)
    if proceeded is not None:
        snapshot = proceeded
        missing = [slug for slug in units if slug not in snapshot]
        if missing:
            raise MigrateError(f"no step-1 snapshot for {', '.join(missing)}; restore the "
                               f"project from git history and migrate again")
    else:
        snapshot = {slug: _legacy_status(project, slug) for slug in units}
    for rel in archived:
        snapshot[rel] = {"status": "archived", "passed": [], "read_only": True}
    plan.say(f"step 1: legacy status for {len(snapshot)} unit(s): {len(units)} to import, "
             f"{len(archived)} archived (read-only)")
    for slug, info in snapshot.items():
        plan.say(f"  {slug}: {info['status']}; gates passed: {', '.join(info['passed']) or '(none)'}")
    return snapshot, units, archived


def _backup(project: Path, backup: Path, plan: Plan) -> None:
    backup.mkdir(parents=True)
    for rel in (LEGACY_STATE, NEW_STATE):
        if (project / rel).is_dir():
            shutil.copytree(project / rel, backup / Path(rel).name, symlinks=True)
            plan.say(f"step 1: backed up {rel}/ to {backup / Path(rel).name}")


def _restore_state(project: Path, backup: Path, existed: dict[str, bool]) -> None:
    for rel in (LEGACY_STATE, NEW_STATE):
        target = project / rel
        if target.exists():
            shutil.rmtree(target)
        if existed.get(rel) and (backup / Path(rel).name).exists():
            shutil.copytree(backup / Path(rel).name, target, symlinks=True)


# ---------------------------------------------------------------- step 2: core drift

def _core_drift(project: Path, stamp: dict, plan: Plan) -> dict[str, bytes]:
    """{legacy core rel: bytes} of every file whose hash differs from the stamp, or that the
    stamp does not know. Each is kept as ``<name>.local`` beside the new layout."""
    files = stamp.get("files", {})
    kept: dict[str, bytes] = {}
    base = project / CORE
    for path in sorted(base.rglob("*")) if base.is_dir() else []:
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(base).as_posix()
        data = path.read_bytes()
        if files.get(f"{CORE}/{rel}") == _sha(data):
            continue
        kept[rel] = data
        known = "differs from its stamp" if f"{CORE}/{rel}" in files else "is not in the stamp"
        plan.say(f"step 2: {CORE}/{rel} {known}; kept as {rel}.local")
        plan.say(f"  replaced by: {SEAMS[rel]}" if rel in SEAMS
                 else "  no new seam is known for this edit: review the .local file by hand")
    if not kept:
        plan.say("step 2: no core drift; every stamped core file matches its stamp")
    return kept


# ---------------------------------------------------------------- step 3: adapter

def _translate(project: Path, plan: Plan) -> tuple[config.Config, dict]:
    raw = yaml.safe_load((project / ADAPTER).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise MigrateError(f"{ADAPTER} is not a mapping")
    try:
        cfg = config_v1.translate(raw, project)
    except config.ConfigError as exc:
        raise MigrateError("the schema-1 adapter does not translate:\n  - "
                           + "\n  - ".join(exc.problems)) from None
    for key in sorted(set(raw) - CONSUMED_V1):
        plan.say(f"step 3: dropped key {key!r} (no schema-2 field)")
    for key in sorted(set(raw.get("workflow") or {}) - CONSUMED_WORKFLOW):
        plan.say(f"step 3: dropped key workflow.{key} (no schema-2 field)")
    for key in sorted(set(raw.get("policies") or {}) - {"deny", "ask"}):
        plan.say(f"step 3: dropped key policies.{key} (no schema-2 field)")
    return cfg, raw


def _schema2(cfg: config.Config, plan: Plan) -> dict:
    """The schema-2 document of a translated configuration. ``{slug}`` becomes ``{unit}``."""
    checks = []
    for check in cfg.checks:
        item = {"id": check.id, "command": check.command.replace("{slug}", "{unit}"),
                "severity": check.severity.value}
        if check.on:
            item["on"] = list(check.on)
        checks.append(item)
    by_id = {check.id: check for check in cfg.checks}
    gates = []
    for gate in cfg.gates:
        item: dict[str, Any] = {"id": gate.id, "needs": list(gate.needs)}
        if gate.checks:
            item["checks"] = list(gate.checks)
        if gate.approval is not None:
            item["approval"] = {"required": bool(gate.approval.required),
                                "channel": gate.approval.channel}
        if gate.surface is not None:
            item["surface"] = {"include": list(gate.surface.include),
                               "exclude": list(gate.surface.exclude)}
        if gate.seals_unit:
            item["seals_unit"] = True
        gates.append(item)
        if gate.approval is not None and gate.approval.required:
            plan.say(f"step 3: gate {gate.id} was approvable by hand: approval.required (tty)")
        elif gate.checks and gate.checks[0] == config_v1.POLICY_CHECK:
            adapter = ", ".join(gate.checks[1:]) or "none"
            plan.say(f"step 3: gate {gate.id}: lint.py --set-gate (retired) is now the policy check "
                     f"and the adapter checks: {adapter}")
        elif gate.checks:
            legacy = by_id[gate.checks[0]].command.replace("{unit}", "{slug}")
            plan.say(f"step 3: gate {gate.id}: legacy producer kept as check {gate.id}: {legacy}")
            surf = gate.surface
            if surf is not None and surf.include:
                plan.say(f"step 3: gate {gate.id} was hashed: surface is the whole unit, "
                         f"excluding {', '.join(surf.exclude) or 'nothing'}")
    doc: dict[str, Any] = {"schema": 2, "project": cfg.project, "unit_noun": cfg.unit_noun,
                           "unit_dir": cfg.unit_dir, "packs": []}
    if checks:
        doc["checks"] = checks
    doc["gates"] = gates
    policy: dict[str, Any] = {}
    if cfg.deny:
        policy["deny"] = [{k: v for k, v in d.items() if k in DENY_KEYS} for d in cfg.deny]
    if cfg.ask:
        policy["ask"] = [{k: v for k, v in d.items() if k in ASK_KEYS} for d in cfg.ask]
    if cfg.protected_paths:
        policy["protected_paths"] = list(cfg.protected_paths)
    if cfg.blind:
        policy["blind"] = list(cfg.blind)
    if policy:
        doc["policy"] = policy
    return doc


def _adapter_text(doc: dict) -> str:
    header = ("# adapter.yaml: the single source of truth for this project's enforcement (schema 2).\n"
              f"# Written by `gategraph migrate` from {Path(ADAPTER_V1).name}, which keeps the original.\n")
    return header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _validate_adapter(project: Path, cfg: config.Config) -> None:
    """Load the written adapter with the schema-2 loader; its gates must be the translation's."""
    try:
        loaded = config.load(project)
    except config.ConfigError as exc:
        raise MigrateError("the schema-2 adapter does not load:\n  - "
                           + "\n  - ".join(exc.problems)) from None

    def shape(c):
        return [(g.id, g.needs, g.surface, g.approval, g.seals_unit) for g in c.gates]

    if shape(loaded) != shape(cfg):
        raise MigrateError("the schema-2 adapter loads, but its gates differ from the translation")


# ---------------------------------------------------------------- step 4: core

def _replace_core(project: Path, journal: Journal, drift: dict[str, bytes], gb, plan: Plan,
                  packs: list[str]) -> None:
    base = project / CORE
    removed = 0
    if base.is_dir():
        for path in sorted(base.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            rel = path.relative_to(base).as_posix()
            if rel in drift:
                journal.write(f"{CORE}/{rel}.local", drift[rel])
            journal.remove(f"{CORE}/{rel}")
            removed += 1
    hashes = {}
    sources = {src.relative_to(gb.CORE_SRC).as_posix(): src for src in gb.core_files()}
    for rel, src in sorted(sources.items()):
        data = src.read_bytes()
        journal.write(f"{CORE}/{rel}", data, mode_from=src)
        hashes[f"{CORE}/{rel}"] = _sha(data)
    plan.say(f"step 4: {CORE}/ replaced: {removed} legacy file(s) out, {len(sources)} new in")
    pack_hashes = {}
    for name in packs:
        src_root = gb.CORE_SRC / gb.CORE_PACKS_DIR / name
        files = {}
        for src in sorted(src_root.rglob("*")):
            rel = src.relative_to(src_root)
            if (not src.is_file() or "__pycache__" in rel.parts or src.suffix == ".pyc"
                    or rel.parts[0] == "examples"):
                continue
            data = src.read_bytes()
            journal.write(f"packs/{name}/{rel.as_posix()}", data, mode_from=src)
            files[rel.as_posix()] = _sha(data)
        pack_hashes[name] = {"files": files}
    manifest = {"version": gb.template_version(), "files": hashes, "packs": pack_hashes}
    journal.text(STAMP, json.dumps(manifest, indent=2) + "\n")
    plan.say(f"step 4: {STAMP} rewritten for v{gb.template_version()}")


def _rewrite_hooks(project: Path, journal: Journal, plan: Plan) -> None:
    """Registrations of harness/core/hooks/*.py point at the host shims, with the same events and
    matchers. A registration with no shim is left as it is and reported."""
    path = project / SETTINGS
    if not path.is_file():
        plan.say("step 4: no .claude/settings.json: no hook registrations to rewrite")
        return
    settings = json.loads(path.read_text())
    changed = []
    for event, groups in (settings.get("hooks") or {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = hook.get("command", "")
                if HOOK_OLD not in command:
                    continue
                script = command.split(HOOK_OLD, 1)[1].split()[0].strip('"')
                if not (TEMPLATE_ROOT / "core" / "hosts" / "shims" / script).is_file():
                    plan.say(f"step 4: {event} hook {script} has no shim; left as it was")
                    continue
                hook["command"] = command.replace(HOOK_OLD, HOOK_NEW)
                changed.append(f"{event}: {script}")
    if changed:
        journal.text(SETTINGS, json.dumps(settings, indent=2) + "\n")
        for item in changed:
            plan.say(f"step 4: hook registration now runs the shim: {item}")


def _policy_scan(project: Path, journal: Journal, plan: Plan) -> None:
    """The schema-2 adapter's policy scan (the seed's checks/policy_scan.py), which the seed
    Makefile runs. Added only when absent: a project's own script is never replaced."""
    rel = "harness/adapter/checks/policy_scan.py"
    if (project / rel).exists():
        return
    src = TEMPLATE_ROOT / "seed" / "checks" / "policy_scan.py"
    journal.write(rel, src.read_bytes(), mode_from=src)
    plan.say(f"step 4: {rel} added (the schema-2 policy scan; the seed Makefile runs it)")


def _merge_gitignore(project: Path, journal: Journal, plan: Plan) -> None:
    seed = (TEMPLATE_ROOT / "seed" / "dot_gitignore").read_text()
    current = (project / GITIGNORE).read_text() if (project / GITIGNORE).exists() else ""
    have = set(current.splitlines())
    missing = [line for line in seed.splitlines() if line and line not in have]
    if missing:
        text = current.rstrip("\n") + ("\n" if current.strip() else "") + "\n".join(missing) + "\n"
        journal.text(GITIGNORE, text)
        plan.say(f"step 4: .gitignore gains {', '.join(missing)}")


# ---------------------------------------------------------------- step 5: environment

def _can_import(python: Path, module: str) -> bool:
    return python.is_file() and _run([python, "-c", f"import {module}"], cwd=TEMPLATE_ROOT).returncode == 0


def _graph_python(project: Path) -> Path | None:
    for variant in (project / ".venv" / "bin" / "python",
                      TEMPLATE_ROOT / ".venv" / "bin" / "python"):
        if _can_import(variant, "langgraph"):
            return variant
    return None


def _build_venv(project: Path) -> bool:
    """`make venv` without make: a venv, the hash-locked install, and the stamp make checks."""
    venv = project / ".venv"
    if not (venv / "bin" / "python").is_file():
        if _run([sys.executable, "-m", "venv", venv], cwd=project).returncode != 0:
            return False
    if _run([venv / "bin" / "python", "-m", "pip", "install", "--require-hashes", "-r",
             project / LOCK], cwd=project).returncode != 0:
        return False
    stamp = project / ENV_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(_sha((project / LOCK).read_bytes()) + "\n")
    return True


def _environment(project: Path, plan: Plan) -> Path:
    """Step 5. Returns the interpreter for the graph steps; raises StopHere when there is none."""
    if (project / ENV_STAMP).is_file() and (project / ".venv" / "bin" / "python").is_file():
        plan.say("step 5: project venv present")
    elif _build_venv(project):
        plan.say("step 5: project venv built from the lock file")
    else:
        plan.say("step 5: project venv could not be built (no network, or pip failed)")
    python = _graph_python(project)
    if python is None:
        raise StopHere("no interpreter with langgraph. Hooks run on the kernel and keep working; "
                       "gate attempts report that the environment is missing. Fix the network or pip "
                       "problem, then run `gategraph migrate --apply` again (it proceeds at step 5)")
    if python != project / ".venv" / "bin" / "python":
        plan.say(f"step 5: graph steps run on the template's locked venv ({python}) until the "
                 "project venv exists")
    return python


# ---------------------------------------------------------------- step 6: units

def _unit_chain(cfg: config.Config, slug: str, legacy: dict, manifest_path: Path,
                project: Path) -> list[dict]:
    """One ``import`` record per gate that legacy status showed as passed, in gate order. The
    surface is taken from disk now, which is correct because legacy status already proved the
    recorded files match it; a gate legacy showed as stale is never imported."""
    state = paths.state_dir(project)
    unit_dir = paths.unit_path(project, cfg.unit_dir, slug)
    data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    legacy_gates = data.get("gates") or {}
    known = {gate.id for gate in cfg.gates}
    unknown = [g for g in legacy["passed"] if g not in known]
    if unknown:
        raise MigrateError(f"{slug}: legacy gate(s) {unknown} are not in the new configuration")
    chain: list[dict] = []
    digest = config.config_digest(cfg)
    for gate in cfg.gates:
        if gate.id not in legacy["passed"]:
            continue
        at = (legacy_gates.get(gate.id) or {}).get("at")
        if at is None:
            at = dt.datetime.fromtimestamp(manifest_path.stat().st_mtime).isoformat()
        surface_digest = surface.store_surface(
            state, surface.hash_surface(unit_dir, gate.id, cfg.gates))
        rec = evidence.make_record(chain, kind="import", gate=gate.id, at=_iso(at),
                                   surface_digest=surface_digest, producers=(),
                                   config_digest=digest, actor=IMPORT_ACTOR)
        chain.append(rec.to_dict())
    return chain


def _import_units(project: Path, cfg: config.Config, units: dict, snapshot: dict,
                  journal: Journal, python: Path, plan: Plan) -> None:
    work = []
    # The `.active` pointer lives beside the units (session.active_path). The new runner removes
    # it when a unit it names goes terminal, so it is noted before the runs.
    journal.note(f"{cfg.unit_dir}/.active")
    for slug, manifest_path in units.items():
        legacy = snapshot[slug]
        chain = _unit_chain(cfg, slug, legacy, manifest_path, project)
        unit_rel = manifest_path.parent.relative_to(project).as_posix()
        for name in ("manifest.yaml", "evidence.jsonl"):
            journal.note(f"{unit_rel}/{name}")
        work.append({"unit": slug, "evidence": chain,
                     "status": STATUS_MAP.get(legacy["status"], "in_progress"),
                     "paused": legacy["status"] == "paused"})
    done = _run([python, "-c", CHILD, project], cwd=project, env=_graph_env(project),
                stdin=json.dumps(work))
    if done.returncode != 0:
        raise MigrateError(f"the graph refused to start the units:\n{done.stdout}{done.stderr}")
    outcome = json.loads(done.stdout.strip().splitlines()[-1])
    for slug in units:
        plan.say(f"step 6: {slug}: thread {outcome[slug]}; {len(snapshot[slug]['passed'])} "
                 f"import record(s); legacy status {snapshot[slug]['status']}")


# ---------------------------------------------------------------- step 7: docs and Makefile

def _gen_docs(project: Path, journal: Journal, plan: Plan) -> None:
    """Regenerate the marked doc blocks with the project's own kernel. Each marked file is noted
    first, so the journal can put it back."""
    for path in sorted(project.rglob("*")):
        rel = path.relative_to(project)
        if not path.is_file() or rel.parts[0] in (".git", ".venv", NEW_STATE):
            continue
        if path.suffix not in (".md", ".yaml", ".txt", ".tmpl"):
            continue
        try:
            if "fh:gen" in path.read_text(encoding="utf-8"):
                journal.note(rel.as_posix())
        except (OSError, UnicodeDecodeError):
            continue
    done = _run([sys.executable, "-m", "core.kernel.gen", "--root", project], cwd=project,
                env=_graph_env(project))
    if done.returncode != 0:
        raise MigrateError(f"doc regeneration refused: {(done.stderr or done.stdout).strip()}")
    for line in done.stdout.splitlines():
        if line.startswith("  regenerated:"):
            plan.say(f"step 7: {line.strip()}")


def _makefile_lines(project: Path, plan: Plan) -> None:
    """Name the Makefile lines that point at legacy paths. The Makefile is never edited."""
    path = project / MAKEFILE
    if not path.is_file():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if "harness/core/" in line and not line.lstrip().startswith("#"):
            plan.say(f"step 7: Makefile line {number} names a legacy path ({line.strip()}); "
                     "the shims serve it. Left unchanged")


# ---------------------------------------------------------------- step 8: verify

def _verify_units(project: Path, python: Path, units: dict, plan: Plan) -> None:
    for slug in units:
        done = _run([python, "harness/core/cli/gate.py", "verify", "--unit", slug], cwd=project,
                    env=_graph_env(project))
        if done.returncode != 0:
            raise MigrateError(f"gate verify of {slug} failed:\n{done.stdout}{done.stderr}")
        plan.say(f"step 8: gate verify --unit {slug}: OK")


def _check_effective(units: dict, snapshot: dict, project: Path, plan: Plan) -> list[str]:
    """Effective gates after the migration against the step-1 snapshot. Returns the differences."""
    differences = []
    for slug, manifest_path in units.items():
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        before, after = sorted(snapshot[slug]["passed"]), sorted(data.get("effective") or [])
        if before != after:
            differences.append(f"{slug}: before {before}, after {after}")
        else:
            plan.say(f"step 8: {slug}: effective gates identical ({', '.join(after) or 'none'})")
    return differences


def _check_make(project: Path, journal: Journal, unit_dir: str, plan: Plan) -> None:
    if _run(["make", "-s", "check"], cwd=project).returncode == 0:
        plan.say("step 8: make check exits 0 with the project's Makefile (left unchanged)")
        return
    seed = (TEMPLATE_ROOT / "seed" / "Makefile").read_text(encoding="utf-8")
    journal.text(MAKEFILE_V2, seed.replace("{{FH_UNIT_DIR}}", unit_dir))
    plan.say("step 8: make check does not run on the migrated layout. Wrote Makefile.v2 beside "
             "the Makefile; the Makefile is untouched. Adopt Makefile.v2 by hand")
    again = _run(["make", "-s", "-f", MAKEFILE_V2, "check"], cwd=project)
    if again.returncode != 0:
        raise MigrateError("make check fails in the migrated project, also through Makefile.v2:\n"
                           + again.stdout + again.stderr)
    plan.say("step 8: make -f Makefile.v2 check exits 0")


def _commit(project: Path, journal: Journal, message: str) -> str:
    """One commit of exactly the paths the journal wrote or removed. Paths git does not know and
    that do not exist are dropped (`git add` would refuse the whole pathspec otherwise). The state
    directory is ignored by git, so it never enters the commit."""
    head = set(_run(["git", "-C", project, "ls-tree", "-r", "--name-only", "HEAD"],
                    cwd=project).stdout.splitlines())
    rels = [rel for rel in journal.paths() if not rel.startswith(NEW_STATE + "/")
            and ((project / rel).exists() or rel in head)]
    if not rels:
        return "no change, so no commit"
    present = [rel for rel in rels if (project / rel).exists()]
    gone = [rel for rel in rels if not (project / rel).exists()]
    if present:
        _run(["git", "-C", project, "add", "-A", "--", *present], cwd=project)
    if gone:
        _run(["git", "-C", project, "rm", "-q", "--cached", "--ignore-unmatch", "--", *gone],
             cwd=project)
    staged = _run(["git", "-C", project, "status", "--porcelain", "--", *rels], cwd=project)
    if not staged.stdout.strip():
        return "no change, so no commit"
    identity = []
    if _run(["git", "-C", project, "config", "user.email"], cwd=project).returncode != 0:
        identity = ["-c", "user.name=gategraph-migrate", "-c", "user.email=migrate@localhost"]
    done = _run(["git", "-C", project, *identity, "commit", "-q", "-m", message, "--", *rels],
                cwd=project)
    if done.returncode != 0:
        raise MigrateError(f"the commit failed:\n{done.stdout}{done.stderr}")
    return "one commit " + _run(["git", "-C", project, "rev-parse", "--short", "HEAD"],
                                cwd=project).stdout.strip()


# ---------------------------------------------------------------- driver

def _adapter_dict(project: Path) -> dict:
    path = project / ADAPTER
    if not path.is_file():
        raise MigrateError(f"no {ADAPTER}: not a stamped project")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MigrateError(f"{ADAPTER} is not a mapping")
    return data


def _migrate(project: Path, apply: bool, allow_dirty: bool, plan: Plan) -> int:
    gb = _gategraph()
    stamp = _preflight(project, allow_dirty, gb, plan)
    adapter = _adapter_dict(project)
    schema1 = "fh_schema" in adapter
    unit_dir = adapter.get("unit_dir") or "notes"
    if not schema1 and not _legacy_units(project, unit_dir)[0]:
        plan.say("already migrated: schema 2 adapter, no version-1 units; nothing to do")
        return 0
    # A run that stopped after step 4 left the step-1 snapshot in the state directory. The
    # legacy command is gone by then, so the proceeded run reads that snapshot instead.
    proceeded = None
    if not schema1 and (project / SNAPSHOT).is_file():
        proceeded = json.loads((project / SNAPSHOT).read_text())
    snapshot, units, archived = _snapshot(project, unit_dir, plan, proceeded)
    if not apply:
        plan.section("dry run: nothing is written")
        _core_drift(project, stamp, plan)
        if schema1:
            cfg, _ = _translate(project, plan)
            _schema2(cfg, plan)
        plan.say(f"step 1: would back up {LEGACY_STATE}/ and {NEW_STATE}/ before any write")
        plan.say(f"step 5: would build the project venv from {LOCK} if it is missing")
        plan.say(f"step 6: would import {len(units)} unit(s); archived and read-only: "
                 f"{', '.join(archived) or 'none'}")
        plan.say("steps 7 and 8: would regenerate docs, verify every unit, run make check, "
                 "and make one commit")
        return 0

    journal = Journal(project)
    stamp_time = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = project.parent / f"{project.name}.migrate-backup-{stamp_time}"
    existed = {rel: (project / rel).is_dir() for rel in (LEGACY_STATE, NEW_STATE)}
    backed_up = False
    try:
        # The backup is taken on every apply, proceeded or not, before any write. A restore only
        # ever copies a backup that completed; a partial copy is never put back.
        backup.parent.mkdir(parents=True, exist_ok=True)
        _backup(project, backup, plan)
        backed_up = True
        plan.say("step 1: to restore, copy the backup back over the state directories")
        if proceeded is None:
            journal.text(SNAPSHOT, json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        message = ""
        try:
            if schema1:
                plan.section("step 2: core drift")
                drift = _core_drift(project, stamp, plan)
                plan.section("step 3: adapter")
                cfg, raw = _translate(project, plan)
                doc = _schema2(cfg, plan)
                journal.note(ADAPTER_V1)
                journal.write(ADAPTER_V1, (project / ADAPTER).read_bytes())
                journal.text(ADAPTER, _adapter_text(doc))
                _validate_adapter(project, cfg)
                plan.say(f"step 3: {ADAPTER_V1} keeps the original; {ADAPTER} is schema 2 and loads")
                plan.section("step 4: core, hooks and ignore rules")
                answers = (yaml.safe_load((project / ANSWERS).read_text())
                           if (project / ANSWERS).exists() else {})
                packs = list((answers or {}).get("packs") or [])
                _replace_core(project, journal, drift, gb, plan, packs)
                _rewrite_hooks(project, journal, plan)
                _merge_gitignore(project, journal, plan)
                _policy_scan(project, journal, plan)
                plan.say("step 4: the old top-level module names are shims over core/compat/")
            cfg = config.load(project)
            plan.section("step 5: environment")
            python = _environment(project, plan)
            plan.section("step 6: units")
            if units:
                _import_units(project, cfg, units, snapshot, journal, python, plan)
            else:
                plan.say("step 6: no version-1 units to import")
            for rel in archived:
                plan.say(f"step 6: {rel} is archived: imported read-only and lazily; nothing written")
            plan.section("step 7: generated docs and the Makefile")
            _gen_docs(project, journal, plan)
            _makefile_lines(project, plan)
            plan.section("step 8: verify")
            if units:
                _verify_units(project, python, units, plan)
                differences = _check_effective(units, snapshot, project, plan)
                if differences:
                    raise MigrateError("effective gates differ from the snapshot: "
                                       + "; ".join(differences))
            _check_make(project, journal, unit_dir, plan)
        except StopHere as exc:
            plan.section("stop")
            plan.say(f"stopped after step 4: {exc}")
            message = ("gategraph migrate: core and adapter (units pending the environment)")
        plan.section("commit")
        plan.say("commit: " + _commit(
            project, journal,
            message or (f"gategraph migrate: v{stamp.get('version')} -> v{gb.template_version()}; "
                        f"{len(units)} unit(s) imported")))
        if message:
            plan.say("next: fix the network or pip problem, then run `gategraph migrate --apply` again (it proceeds at step 5)")
        return 0
    except Exception as exc:
        journal.rollback()
        if backed_up:
            _restore_state(project, backup, existed)
        if isinstance(exc, MigrateError):
            plan.say(f"ROLLED BACK: {exc}")
        else:
            plan.say(f"ROLLED BACK after an internal error: {type(exc).__name__}: {exc}")
        return 1


def run(project_arg, *, apply: bool = False, allow_dirty: bool = False) -> tuple[int, Plan]:
    """Run the migration. Returns (exit code, plan). A dry run unless ``apply``."""
    project = Path(project_arg).resolve()
    plan = Plan()
    plan.say(f"gategraph migrate {project}: " + ("apply" if apply else "dry run (default)"))
    try:
        code = _migrate(project, apply, allow_dirty, plan)
    except MigrateError as exc:
        plan.say(f"REFUSED: {exc}")
        code = 1
    return code, plan


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gategraph.py migrate")
    parser.add_argument("dir")
    parser.add_argument("--apply", action="store_true", help="perform the steps and commit once")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="permit a dirty tree (the commit holds only the migration's paths)")
    args = parser.parse_args(argv)
    code, plan = run(args.dir, apply=args.apply, allow_dirty=args.allow_dirty)
    print("\n".join(plan.lines))
    return code


__all__ = ["MigrateError", "StopHere", "main", "run"]
