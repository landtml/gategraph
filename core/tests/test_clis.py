"""CLI tests: gate.py, lint.py, doctor.py driven exactly as an agent drives
them (subprocess + exit codes), against throwaway fixture roots."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from fixtures import (ACCT_TOKEN, default_adapter, mkroot, run_cli,  # noqa: E402
                      write_adapter)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def gate(self, *args, **kw):
        return run_cli("gate.py", *args, root=self.root, **kw)

    def lint(self, *args, **kw):
        return run_cli("lint.py", *args, root=self.root, **kw)

    def start(self, slug="u1", sid="S1"):
        return self.gate("start", "--unit", slug, "--session-id", sid)

    def manifest_data(self, slug="u1") -> dict:
        return yaml.safe_load(
            (self.root / "units" / slug / "manifest.yaml").read_text())


class TestGateCli(Base):
    def test_start_creates_unit_active_and_marker(self):
        r = self.start()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.root / "units/u1/manifest.yaml").is_file())
        active = yaml.safe_load((self.root / "units/.active").read_text())
        self.assertEqual(active["slug"], "u1")
        self.assertTrue((self.root / "harness/state/touched-S1").exists())

    def test_second_start_blocked_while_first_active(self):
        self.start("u1")
        r = self.start("u2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("u1", r.stderr)

    def test_start_after_pause_takes_over(self):
        self.start("u1")
        self.gate("pause", "--unit", "u1")
        r = self.start("u2")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_approve_walks_human_gates_in_order(self):
        self.start()
        r = self.gate("approve", "composed", "--unit", "u1")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = self.gate("approve", "checked", "--unit", "u1")
        self.assertEqual(r.returncode, 0, r.stderr)
        gates = self.manifest_data()["gates"]
        self.assertIn("composed", gates)
        self.assertIn("checked", gates)

    def test_approve_out_of_order_rejected(self):
        self.start()
        r = self.gate("approve", "checked", "--unit", "u1")
        self.assertEqual(r.returncode, 1)

    def test_tool_set_gate_refuses_manual_approve(self):
        self.start()
        self.gate("approve", "composed", "--unit", "u1")
        self.gate("approve", "checked", "--unit", "u1")
        r = self.gate("approve", "verified", "--unit", "u1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("evidence", (r.stdout + r.stderr).lower())
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_status_names_next_gate_command_from_config(self):
        self.start()
        r = self.gate("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        # the next-step text must come from adapter.yaml gate_commands
        self.assertIn("gate.py approve composed --unit u1", r.stdout)

    def test_escalate_requires_note_and_is_terminal(self):
        self.start()
        r = self.gate("escalate", "--unit", "u1")
        self.assertEqual(r.returncode, 2)  # argparse: --note required
        r = self.gate("escalate", "--unit", "u1", "--note",
                      "3 failures on checked; blocked on missing credentials")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.manifest_data()["status"], "escalated")
        self.assertTrue((self.root / "units/u1/escalation.md").is_file())
        self.assertFalse((self.root / "units/.active").exists())

    def test_abandon_clears_active(self):
        self.start()
        r = self.gate("abandon", "--unit", "u1")
        self.assertEqual(r.returncode, 0)
        self.assertFalse((self.root / "units/.active").exists())
        self.assertEqual(self.manifest_data()["status"], "abandoned")

    def test_proceed_refuses_abandoned(self):
        self.start()
        self.gate("abandon", "--unit", "u1")
        r = self.gate("proceed", "--unit", "u1")
        self.assertEqual(r.returncode, 1)

    def test_repair_rebuilds_corrupt_manifest(self):
        self.start()
        mpath = self.root / "units/u1/manifest.yaml"
        mpath.write_text("{ corrupt: [yaml\n")
        r = self.gate("repair", "--unit", "u1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.manifest_data()["status"], "in_progress")
        self.assertTrue((self.root / "units/u1/manifest.yaml.corrupt").exists())


class TestLintCli(Base):
    def _walk_to_verified(self):
        self.start()
        (self.root / "units/u1/note.md").write_text("a clean research note\n")
        self.gate("approve", "composed", "--unit", "u1")
        self.gate("approve", "checked", "--unit", "u1")

    def test_clean_unit_exit0(self):
        self._walk_to_verified()
        r = self.lint("--unit", "u1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_set_gate_records_hashed_evidence(self):
        self._walk_to_verified()
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gates = self.manifest_data()["gates"]
        self.assertIn("verified", gates)
        self.assertIn("note.md", gates["verified"]["sha256"])

    def test_set_gate_refused_on_empty_unit(self):
        # F36(a): a tool-set gate earned over ZERO files is vacuous, the
        # adapter checks never ran and the sha256 map would be empty.
        self.start()
        self.gate("approve", "composed", "--unit", "u1")
        self.gate("approve", "checked", "--unit", "u1")
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no files", (r.stdout + r.stderr).lower())
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_staleness_reopens_after_added_file(self):
        # F36(b), end to end: an addition shows up as STALE in gate status.
        self._walk_to_verified()
        self.lint("--unit", "u1", "--set-gate", "verified")
        (self.root / "units/u1/appendix.md").write_text(
            "added after verification\n")
        r = self.gate("status")
        self.assertIn("STALE", r.stdout.upper())

    def test_staleness_reopens_after_edit(self):
        self._walk_to_verified()
        self.lint("--unit", "u1", "--set-gate", "verified")
        (self.root / "units/u1/note.md").write_text("edited after verification\n")
        r = self.gate("status")
        self.assertIn("verified", r.stdout)   # named as next/stale again
        self.assertIn("STALE", r.stdout.upper())

    def test_fatal_finding_exit2_and_no_gate(self):
        self._walk_to_verified()
        (self.root / "units/u1/note.md").write_text("id " + ACCT_TOKEN + "\n")
        r = self.lint("--unit", "u1", "--set-gate", "verified")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_advisory_finding_exit1(self):
        self._walk_to_verified()
        (self.root / "units/u1/note.md").write_text("ADVISORY-MARKER here\n")
        r = self.lint("--unit", "u1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)

    def test_single_file_mode_no_gate_side_effect(self):
        self._walk_to_verified()
        f = self.root / "units/u1/note.md"
        r = self.lint("--file", str(f))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("verified", self.manifest_data()["gates"])

    def test_adapter_check_fatal_blocks(self):
        check = self.root / "harness/adapter/checks/no_failcheck.py"
        check.write_text(
            "import sys\nfrom pathlib import Path\n"
            "bad = [f for f in sys.argv[1:] if 'FAILCHECK' in "
            "Path(f).read_text(errors='replace')]\n"
            "print('\\n'.join(bad), file=sys.stderr)\n"
            "sys.exit(1 if bad else 0)\n")
        adapter = default_adapter()
        adapter["checks"] = [{"id": "no-failcheck",
                              "cmd": "python3 harness/adapter/checks/no_failcheck.py {files}",
                              "on": ["units/**/*.md"], "severity": "fatal"}]
        write_adapter(self.root, adapter)
        self._walk_to_verified()
        (self.root / "units/u1/note.md").write_text("FAILCHECK sentinel\n")
        r = self.lint("--unit", "u1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("no-failcheck", r.stdout + r.stderr)

    def test_refs_flags_unresolved_wikilink(self):
        (self.root / "MEMORY.md").write_text("see [[missing-doc]]\n")
        r = self.lint("refs")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("missing-doc", r.stdout)

    def test_refs_clean_when_wikilinks_resolve(self):
        (self.root / "MEMORY.md").write_text("see [[CLAUDE]]\n")
        r = self.lint("refs")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestDoctorCli(Base):
    def test_ok_with_advisory_failure(self):
        r = run_cli("doctor.py", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("missing-tool", r.stdout)

    def test_required_failure_exit1(self):
        adapter = default_adapter()
        adapter["doctor"]["required"].append(
            {"id": "ghost", "cmd": "definitely-not-a-real-binary-fh --version"})
        write_adapter(self.root, adapter)
        r = run_cli("doctor.py", root=self.root)
        self.assertEqual(r.returncode, 1)

    def test_json_mode_and_cache(self):
        r = run_cli("doctor.py", "--json", root=self.root)
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])
        cache = self.root / "harness/state/doctor.json"
        self.assertTrue(cache.is_file(), "doctor must cache results for session_start")


class TestGateSessionLock(Base):
    """Part 1 item 5: the unit of concurrency is ONE session per project root.
    gate.py mutations take an advisory flock; a second racing session is
    refused with a message naming the winner's session."""

    HOLDER = (
        "import sys, time\n"
        "sys.path.insert(0, {core_parent!r})\n"
        "from pathlib import Path\n"
        "from core import config, paths\n"
        "root = Path({root!r})\n"
        "cfg = config.load(root)\n"
        "with paths.session_lock(root, cfg):\n"
        "    print('READY', flush=True)\n"
        "    sys.stdin.readline()\n"
    )

    def _hold_lock(self):
        import subprocess
        proc = subprocess.Popen(
            [sys.executable, "-c",
             self.HOLDER.format(core_parent=str(Path(__file__).resolve().parents[2]),
                                root=str(self.root))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = proc.stdout.readline()
        self.assertIn("READY", line, "holder process failed to take the lock")
        return proc

    def test_racing_mutation_refused_and_names_winner(self):
        self.start("u1", sid="winner-sess")
        proc = self._hold_lock()
        try:
            r = self.gate("approve", "composed", "--unit", "u1", "--by", "loser")
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertIn("winner-sess", r.stderr,
                          "refusal must name the winning session")
            m = self.manifest_data()
            self.assertNotIn("composed", m["gates"],
                             "the losing approve must not have mutated state")
        finally:
            proc.stdin.write("\n")
            proc.stdin.close()
            proc.wait(timeout=10)
        # Lock released, the same command now succeeds.
        r = self.gate("approve", "composed", "--unit", "u1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_status_is_not_blocked_by_the_lock(self):
        self.start("u1", sid="winner-sess")
        proc = self._hold_lock()
        try:
            r = self.gate("status")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("u1", r.stdout)
        finally:
            proc.stdin.write("\n")
            proc.stdin.close()
            proc.wait(timeout=10)


class TestDoctorClaudePin(Base):
    """Part 1 item 7: for loop-enabled projects doctor gains a built-in
    ADVISORY check, `claude` CLI present and matching the FLAGS.md pin."""

    @classmethod
    def setUpClass(cls):
        import re
        flags = (Path(__file__).resolve().parents[1] / "loop" / "FLAGS.md")
        m = re.search(r"CLI (\d+\.\d+\.\d+)", flags.read_text(encoding="utf-8"))
        assert m, "FLAGS.md must state the verified CLI version"
        cls.pin = m.group(1)

    def _doctor_json(self, fake_version=None):
        """Run doctor with a hermetic PATH: python3 plus a fake `claude`
        (or no claude at all, the host may have a real one installed)."""
        import os
        import shutil
        import subprocess
        adapter = default_adapter()
        adapter["loop"]["enabled"] = True
        write_adapter(self.root, adapter)
        bindir = self.root / "fakebin"
        bindir.mkdir(exist_ok=True)
        (bindir / "python3").symlink_to(shutil.which("python3"))
        if fake_version is not None:
            sh = bindir / "claude"
            sh.write_text(f"#!/bin/sh\necho '{fake_version} (Claude Code)'\n")
            sh.chmod(0o755)
        env = {**os.environ, "PATH": str(bindir)}
        core = Path(__file__).resolve().parents[1]
        r = subprocess.run(
            [sys.executable, str(core / "doctor.py"), "--json",
             "--root", str(self.root)],
            capture_output=True, text=True, env=env)
        return r, {c["name"]: c for c in json.loads(r.stdout)["checks"]}

    def test_matching_pin_is_ok(self):
        r, checks = self._doctor_json(fake_version=self.pin)
        self.assertIn("claude-cli", checks)
        self.assertTrue(checks["claude-cli"]["ok"], checks["claude-cli"])
        self.assertFalse(checks["claude-cli"]["required"], "must be advisory")

    def test_drifted_version_warns_with_pin_named(self):
        r, checks = self._doctor_json(fake_version="9.9.9")
        self.assertEqual(r.returncode, 0, "advisory drift must not fail doctor")
        self.assertFalse(checks["claude-cli"]["ok"])
        self.assertIn(self.pin, checks["claude-cli"]["detail"])
        self.assertIn("FLAGS.md", checks["claude-cli"]["detail"])

    def test_missing_cli_warns(self):
        r, checks = self._doctor_json(fake_version=None)
        self.assertEqual(r.returncode, 0, "missing CLI is advisory, not fatal")
        self.assertFalse(checks["claude-cli"]["ok"])
        self.assertIn("not found", checks["claude-cli"]["detail"])

    def test_loop_disabled_has_no_claude_check(self):
        r = run_cli("doctor.py", "--json", root=self.root)
        names = [c["name"] for c in json.loads(r.stdout)["checks"]]
        self.assertNotIn("claude-cli", names,
                         "the pin check is for loop-enabled projects only")


class TestDoctorPlatform(Base):
    """Part 1 item 3: POSIX-only is declared and enforced, doctor fails
    politely on win32 instead of letting POSIX assumptions fail obscurely."""

    def _platform_result(self):
        from core import doctor
        res = doctor.run_checks(self.root)
        return next(r for r in res if r.name == "platform")

    def test_win32_is_a_required_failure(self):
        from unittest import mock
        with mock.patch("sys.platform", "win32"):
            plat = self._platform_result()
        self.assertTrue(plat.required, "platform check must be REQUIRED")
        self.assertFalse(plat.ok)
        self.assertIn("WSL", plat.detail)

    def test_posix_passes(self):
        plat = self._platform_result()
        self.assertTrue(plat.ok, plat.detail)
        self.assertTrue(plat.required)


if __name__ == "__main__":
    unittest.main(verbosity=2)
