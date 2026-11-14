"""M3: the version-1 compatibility shims (plan 1.18, section "How legacy producers work after
migration"; bug B01 for migrated projects).

A migrated project keeps its version-1 adapter scripts. They import ``core.config``,
``core.manifest``, ``core.paths`` and friends from ``harness/core/``. The migration replaces that
directory with the new core (including ``compat/``) and the old module names become shims. Every
test here builds such a project in a temporary directory and runs the scripts in subprocesses.

Offline only: the checks are shell builtins and the sample script, no model and no network.

Run standalone: ``.venv/bin/python core/tests/compat/test_v1_api.py``.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from core import brand

REPO = _bootstrap.REPO
CORE = REPO / "core"
# The version-1 sample producer, unchanged: the reference `review_gate.py` as it stood at HEAD
# before the schema-2 rewrite (that rewrite is a check that never sets a gate). Frozen as a
# fixture so the legacy-producer contract stays under test.
SAMPLE = CORE / "tests" / "kernel" / "fixtures" / "legacy_review_gate_v1.py"
LEGACY_MODULES = ("config", "manifest", "paths", "lint", "policy", "doctor", "failures", "gate")
UNIT = "u1"
STATE = brand.STATE_DIRNAME
SHIM_LINE = "deprecated version-1 shim"

ADAPTER = """\
schema: 2
project: demo
unit_noun: note
unit_dir: units
checks:
  - {id: composed, command: "true", severity: fatal}
  - id: reviewed
    command: "%(py)s harness/adapter/checks/review_gate.py --unit {unit}"
    severity: fatal
gates:
  - id: composed
    checks: [composed]
    repair: {max_attempts: 1}
  - id: reviewed
    needs: [composed]
    checks: [reviewed]
    repair: {max_attempts: 1}
"""

ROGUE = """\
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from core import config, manifest  # the version-1 names, through the shims
root = Path(__file__).resolve().parents[3]
cfg = config.load(root)
m = manifest.load_for(root, cfg, sys.argv[1])
try:
    m.set_gate("reviewed", sha256={})
except manifest.ManifestError as exc:
    print("refused:", exc)
    sys.exit(3)
print("set")
"""

PROBE = """\
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness"))
from core import config, manifest
root = Path(__file__).resolve().parents[3]
for _ in range(2):
    cfg = config.load(root)
    manifest.load_for(root, cfg, sys.argv[1])
"""


def env_for_child():
    return {k: v for k, v in os.environ.items()
            if not k.startswith((brand.ENV_PREFIX, *brand.LEGACY_ENV_PREFIXES))}


def build_migrated_project(tmp: Path) -> Path:
    """A migrated project: the new core under harness/core (legacy names are shims), the
    version-1 sample producer under harness/adapter/checks, and one unit with its artifact."""
    root = Path(tmp)
    harness = root / "harness"
    shutil.copytree(CORE, harness / "core",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in LEGACY_MODULES:  # the old top-level names become shims over compat
        (harness / "core" / f"{name}.py").write_text(
            f"\"\"\"Version-1 name kept as a shim (M3).\"\"\"\n"
            f"from core.compat.shims.{name} import *  # noqa: F401,F403\n", encoding="utf-8")
    checks = harness / "adapter" / "checks"
    checks.mkdir(parents=True)
    shutil.copy(SAMPLE, checks / "review_gate.py")
    (harness / "adapter" / "adapter.yaml").write_text(
        ADAPTER % {"py": sys.executable}, encoding="utf-8")
    unit = root / "units" / UNIT
    unit.mkdir(parents=True)
    note = b"# A note\n\nBody text.\n"
    (unit / "note.md").write_bytes(note)
    review = {"artifact_sha256": hashlib.sha256(note).hexdigest(), "verdict": "pass",
              "findings": []}
    (unit / "review.json").write_text(json.dumps(review), encoding="utf-8")
    return root


def run_gate(root: Path, *args):
    cli = root / "harness" / "core" / "cli" / "gate.py"
    return subprocess.run([sys.executable, str(cli), "--root", str(root), *args],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=600, env=env_for_child(), cwd=str(root))


def run_script(root: Path, rel: str, *args):
    return subprocess.run([sys.executable, str(root / rel), *args],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=600, env=env_for_child(), cwd=str(root))


def effective(root: Path) -> list:
    data = yaml.safe_load((root / "units" / UNIT / "manifest.yaml").read_text(encoding="utf-8"))
    return list(data.get("effective") or [])


def hooks_log(root: Path) -> str:
    path = root / "harness" / "logs" / "hooks.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestV1Api(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = build_migrated_project(Path(self._tmp.name))
        started = run_gate(self.root, "start", "--unit", UNIT, "--session-id", "S1")
        self.assertEqual(started.returncode, 0, started.stderr)

    def test_contract_names_are_importable(self):
        from core.compat import v1
        names = {
            "config": ("load", "ConfigError"),
            "manifest": ("load_for", "ManifestError", "sha256_file", "Manifest"),
            "paths": ("unit_path", "read_active", "touch_marker", "manifest_path", "state_dir",
                      "find_repo_root", "clear_active"),
            "policy": ("is_protected", "compile_policies", "scan_text", "scan_file"),
            "lint": ("unit_files", "scan_files", "run_adapter_checks"),
            "doctor": ("read_cache",),
            "failures": ("record",),
        }
        for module, attrs in names.items():
            mod = getattr(v1, module)
            for attr in attrs:
                self.assertTrue(hasattr(mod, attr), f"{module}.{attr}")
        for attr in ("set_gate", "save", "next_gate", "effective_gates"):
            self.assertTrue(hasattr(v1.Manifest, attr), f"Manifest.{attr}")

    def test_sample_script_runs_unchanged_and_seals_through_the_graph(self):
        first = run_script(self.root, "harness/adapter/checks/review_gate.py", "--unit", UNIT)
        self.assertNotEqual(first.returncode, 0, "gate attempted before its predecessor")
        attempt = run_gate(self.root, "attempt", "composed", "--unit", UNIT)
        self.assertEqual(attempt.returncode, 0, attempt.stderr)
        self.assertEqual(effective(self.root), ["composed"])

        proc = run_script(self.root, "harness/adapter/checks/review_gate.py", "--unit", UNIT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("reviewed set for u1", proc.stdout)
        self.assertEqual(effective(self.root), ["composed", "reviewed"])

        evidence = (self.root / "units" / UNIT / "evidence.jsonl").read_text(encoding="utf-8")
        gate_rows = [json.loads(line) for line in evidence.splitlines()
                     if json.loads(line).get("gate") == "reviewed"]
        self.assertEqual(len(gate_rows), 1)
        self.assertEqual(gate_rows[0]["producers"][0]["id"], "reviewed")

    def test_B01_shim_refuses_wrong_producer(self):
        attempt = run_gate(self.root, "attempt", "composed", "--unit", UNIT)
        self.assertEqual(attempt.returncode, 0, attempt.stderr)
        rogue = self.root / "harness" / "adapter" / "checks" / "rogue.py"
        rogue.write_text(ROGUE, encoding="utf-8")
        proc = run_script(self.root, "harness/adapter/checks/rogue.py", UNIT)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("not the registered legacy producer", proc.stdout)
        self.assertNotIn("reviewed", effective(self.root))

    def test_B01_refuses_a_call_made_from_the_test_process(self):
        from core.compat.v1 import config, manifest
        cfg = config.load(self.root)
        m = manifest.load_for(self.root, cfg, UNIT)
        with self.assertRaises(manifest.ManifestError):
            m.set_gate("reviewed", sha256={})
        self.assertNotIn("reviewed", effective(self.root))

    def test_deprecation_line_once_per_process_and_shim(self):
        probe = self.root / "harness" / "adapter" / "checks" / "probe.py"
        probe.write_text(PROBE, encoding="utf-8")
        proc = run_script(self.root, "harness/adapter/checks/probe.py", UNIT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [line for line in hooks_log(self.root).splitlines() if SHIM_LINE in line]
        # config.load and manifest.load_for each called twice: one line per shim, not four.
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(any("config.load" in line for line in lines), lines)
        self.assertTrue(any("manifest.load_for" in line for line in lines), lines)

    def test_missing_adapter_raises_the_version_1_config_error(self):
        from core.compat.v1 import config
        empty = Path(tempfile.mkdtemp(dir=self._tmp.name))
        with self.assertRaises(config.ConfigError):
            config.load(empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
