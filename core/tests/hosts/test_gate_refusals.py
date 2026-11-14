"""The gate command, driven as the agent drives it, ported from the legacy core/tests/test_boundary.py
(X1 B5b). Each case is a typed refusal or an honest pass, never a traceback and never a gate earned
by a check that did not run (B01 class: the legacy `lint --set-gate` and `approve` are retired by
D11, so the new flow `gate attempt` must refuse what they used to wave through).

These run core/cli/gate.py on the venv (langgraph). When .venv is absent they skip: `make venv`
builds it, and test-core builds it before its graph layer.

Run alone with: .venv/bin/python core/tests/hosts/test_gate_refusals.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import os
import pty
import random
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from core import brand

REPO = Path(_bootstrap.REPO)
VENV_PY = REPO / ".venv" / "bin" / "python"
CLI = REPO / "core" / "cli" / "gate.py"
UNIT = "u1"

ADAPTER = """\
schema: 2
project: demo
unit_noun: report
unit_dir: units
checks:
  - {id: c1, command: "%(command)s", severity: %(severity)s}
  - {id: c2, command: "true", severity: fatal}
gates:
  - id: g1
    checks: [c1]
    repair: {max_attempts: 1}
  - id: g2
    needs: [g1]
    checks: [c2]
"""


def make_project(tmp, command="true", severity="fatal"):
    root = Path(tmp)
    (root / "harness" / "adapter").mkdir(parents=True)
    (root / "harness" / "adapter" / "adapter.yaml").write_text(
        ADAPTER % {"command": command, "severity": severity}, encoding="utf-8")
    (root / "units").mkdir()
    return root


def gate(root, *args, tty=False):
    """Run the gate CLI. ``tty=True`` gives it a pseudo-terminal as stdin, which the answers
    to an escalation require (its channel is tty)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith((brand.ENV_PREFIX, *brand.LEGACY_ENV_PREFIXES))}
    cmd = [str(VENV_PY), str(CLI), "--root", str(root), *args]
    if not tty:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=300, env=env, cwd=str(root))
    master, slave = pty.openpty()
    try:
        return subprocess.run(cmd, stdin=slave, capture_output=True, text=True,
                              timeout=300, env=env, cwd=str(root))
    finally:
        os.close(slave)
        os.close(master)


def seed_unit(root, unit=UNIT):
    """A real file inside g1's surface. A hashed gate over an empty unit is refused
    (seal.empty_surface), so each attempt that should pass needs one first."""
    path = Path(root) / "units" / unit / "report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("sketch\n", encoding="utf-8")


def manifest(root, unit=UNIT):
    return yaml.safe_load((Path(root) / "units" / unit / "manifest.yaml").read_text(encoding="utf-8"))


@unittest.skipUnless(VENV_PY.is_file(), "no .venv: run `make venv` (the graph layer needs it)")
class GateRefusals(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name

    def project(self, **kw):
        root = make_project(Path(self.tmp) / f"p{len(list(Path(self.tmp).iterdir()))}", **kw)
        return root

    def assert_no_traceback(self, r):
        self.assertNotIn("Traceback", r.stdout + r.stderr, r.stderr)


class TestHostileSlugs(GateRefusals):
    BAD_SLUGS = ["../../fz-escape", "units/nested", "a/../../b", "-leading-dash", ".hidden", "", "a b"]

    def test_start_refuses_hostile_slugs_and_creates_nothing(self):
        root = self.project()
        targets = {s: (root / "units" / s).resolve() for s in self.BAD_SLUGS if s}
        before = {s: t.exists() for s, t in targets.items()}
        for slug in self.BAD_SLUGS:
            r = gate(root, "start", "--unit", slug)
            self.assertNotEqual(r.returncode, 0, f"slug {slug!r} accepted")
            self.assert_no_traceback(r)
        for slug, t in targets.items():
            self.assertEqual(t.exists(), before[slug], f"slug {slug!r} created {t}")
        self.assertFalse((root / "units" / "units").exists())

    def test_random_slug_sweep_never_tracebacks(self):
        root = self.project()
        rng = random.Random(99)
        chars = "abz09-_./\\ $;\n"
        slugs = {"".join(rng.choice(chars) for _ in range(rng.randint(1, 12))) for _ in range(20)}
        for slug in slugs:
            r = gate(root, "start", "--unit", slug)
            self.assertIn(r.returncode, (0, 1, 2), repr(slug))
            self.assert_no_traceback(r)


class TestStartIsIdempotent(GateRefusals):
    def test_restart_preserves_earned_gates(self):
        root = self.project()
        self.assertEqual(gate(root, "start", "--unit", UNIT).returncode, 0)
        seed_unit(root)
        self.assertEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 0)
        self.assertEqual(manifest(root)["effective"], ["g1"])
        r = gate(root, "start", "--unit", UNIT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(manifest(root)["effective"], ["g1"], "re-running start destroyed evidence")
        self.assertIn("evidence is kept", r.stdout + r.stderr)

    def test_start_on_abandoned_unit_refused(self):
        root = self.project(command="false")  # a failing check pauses for a person (escalation)
        gate(root, "start", "--unit", UNIT)
        self.assertEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 1)
        self.assertEqual(manifest(root)["status"], "paused")
        self.assertEqual(gate(root, "proceed", "--unit", UNIT, "--decision", "abandon",
                              "--by", "alice", "--note", "stuck", tty=True).returncode, 0)
        self.assertEqual(manifest(root)["status"], "abandoned")
        r = gate(root, "start", "--unit", UNIT)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(manifest(root)["status"], "abandoned")
        self.assert_no_traceback(r)


class TestTypedRefusals(GateRefusals):
    def test_attempt_missing_unit_is_typed(self):
        root = self.project()
        r = gate(root, "attempt", "g1", "--unit", "no-such-unit")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_no_traceback(r)

    def test_pause_and_escalate_missing_unit_are_typed(self):
        root = self.project()
        for args in (("pause", "--unit", "typo"),
                     ("escalate", "--unit", "typo", "--note", "x")):
            r = gate(root, *args)
            self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assert_no_traceback(r)


class TestCheckContractEarnsNoGateByAccident(GateRefusals):
    """F19 (B01 class), re-expressed for the new flow: `gate attempt` is the only way to earn a gate."""

    def earned(self, root):
        return "g1" in (manifest(root).get("effective") or [])

    def test_missing_binary_refuses_the_gate(self):
        root = self.project(command="this_binary_does_not_exist_fh_xyz {files}")
        gate(root, "start", "--unit", UNIT)
        self.assertNotEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 0)
        self.assertFalse(self.earned(root), "a check that did not run earned the gate")

    def test_signal_death_refuses_the_gate(self):
        root = self.project(command="sh -c 'kill -9 $$'")
        gate(root, "start", "--unit", UNIT)
        self.assertNotEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 0)
        self.assertFalse(self.earned(root))

    def test_exit_2_is_a_warning_and_the_gate_passes(self):
        root = self.project(command="sh -c 'echo advisory-detail >&2; exit 2'")
        gate(root, "start", "--unit", UNIT)
        seed_unit(root)
        self.assertEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 0)
        self.assertTrue(self.earned(root))

    def test_severity_warn_demotes_exit_1(self):
        root = self.project(command="sh -c 'echo style nit; exit 1'", severity="warn")
        gate(root, "start", "--unit", UNIT)
        seed_unit(root)
        self.assertEqual(gate(root, "attempt", "g1", "--unit", UNIT).returncode, 0)
        self.assertTrue(self.earned(root))


if __name__ == "__main__":
    unittest.main(verbosity=2)
