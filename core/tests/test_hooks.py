"""Hook tests: pretool_guard, posttool_lint, stop_gate, session_start driven
exactly as Claude Code drives them, JSON event on stdin, $CLAUDE_PROJECT_DIR
set, against throwaway fixture roots."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fixtures import (ACCT_TOKEN, age_file, mkroot, post_event, pre_event,  # noqa: E402
                      run_cli, run_hook, stop_event)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def start(self, slug="u1", sid="S1"):
        r = run_cli("gate.py", "start", "--unit", slug, "--session-id", sid,
                    root=self.root)
        assert r.returncode == 0, r.stderr
        return self.root / "units" / slug

    def consolidate_handoff(self, sid="S1"):
        """Refresh HANDOFF.md so only the workflow condition is under test."""
        from core import paths
        marker = paths.marker_path(self.root, sid)
        if marker.exists():
            age_file(marker, 60)
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\nconsolidated\n\n## Next\ncontinue\n")

    def decision(self, proc):
        out = json.loads(proc.stdout)
        return out["hookSpecificOutput"]["permissionDecision"], \
            out["hookSpecificOutput"]["permissionDecisionReason"]


class TestPretoolGuard(Base):
    def test_deny_policy_content_denied_anywhere(self):
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "docs/x.md"),
                        "content": "id: " + ACCT_TOKEN + "\n"})
        r = run_hook("pretool_guard.py", ev, self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        decision, reason = self.decision(r)
        self.assertEqual(decision, "deny")
        self.assertIn("acct", reason.lower())

    def test_pretool_false_policy_not_denied_here(self):
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "units/u1/note.md"),
                        "content": "ADVISORY-MARKER\n"})
        r = run_hook("pretool_guard.py", ev, self.root)
        if r.stdout.strip():
            decision, _ = self.decision(r)
            self.assertNotEqual(decision, "deny")

    def test_protected_path_denied(self):
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "units/u1/manifest.yaml"),
                        "content": "slug: u1\n"})
        decision, reason = self.decision(run_hook("pretool_guard.py", ev, self.root))
        self.assertEqual(decision, "deny")
        self.assertIn("harness", reason.lower())

    def test_ask_path(self):
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "templates/base.tex"),
                        "content": "clean\n"})
        decision, _ = self.decision(run_hook("pretool_guard.py", ev, self.root))
        self.assertEqual(decision, "ask")

    def test_clean_write_allowed_silently(self):
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "units/u1/note.md"),
                        "content": "a clean line about physics\n"})
        r = run_hook("pretool_guard.py", ev, self.root)
        self.assertEqual(r.returncode, 0)
        if r.stdout.strip():
            decision, _ = self.decision(r)
            self.assertIn(decision, (None, "allow"))

    def test_malformed_stdin_fails_open(self):
        r = run_hook("pretool_guard.py", "{not json", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unloadable_config_fails_closed_for_unit_writes(self):
        (self.root / "harness/adapter/adapter.yaml").write_text("{ corrupt: [\n")
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "units/u1/note.md"),
                        "content": "anything\n"})
        decision, reason = self.decision(run_hook("pretool_guard.py", ev, self.root))
        self.assertEqual(decision, "deny")
        self.assertIn("adapter", reason.lower())

    def test_unloadable_config_still_allows_fixing_adapter(self):
        (self.root / "harness/adapter/adapter.yaml").write_text("{ corrupt: [\n")
        ev = pre_event(self.root, "Write",
                       {"file_path": str(self.root / "harness/adapter/adapter.yaml"),
                        "content": "fh_schema: 1\n"})
        r = run_hook("pretool_guard.py", ev, self.root)
        if r.stdout.strip():
            decision, _ = self.decision(r)
            self.assertNotEqual(decision, "deny")


class TestPosttoolLint(Base):
    def test_fatal_finding_exit2(self):
        unit = self.start()
        note = unit / "note.md"
        note.write_text("id " + ACCT_TOKEN + "\n")
        ev = post_event(self.root, "Write",
                        {"file_path": str(note), "content": note.read_text()})
        r = run_hook("posttool_lint.py", ev, self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("acct", (r.stdout + r.stderr).lower())

    def test_advisory_finding_exit0_with_note(self):
        unit = self.start()
        note = unit / "note.md"
        note.write_text("ADVISORY-MARKER present\n")
        ev = post_event(self.root, "Write",
                        {"file_path": str(note), "content": note.read_text()})
        r = run_hook("posttool_lint.py", ev, self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("todo-advisory", r.stdout)

    def test_write_arms_stop_gate_marker(self):
        unit = self.start(sid="S1")
        note = unit / "note.md"
        note.write_text("clean\n")
        ev = post_event(self.root, "Write",
                        {"file_path": str(note), "content": "clean\n"}, sid="S9")
        run_hook("posttool_lint.py", ev, self.root)
        self.assertTrue((self.root / "harness/state/touched-S9").exists())

    def test_bash_relint_noop_without_active_unit(self):
        ev = post_event(self.root, "Bash", {"command": "echo hi"})
        r = run_hook("posttool_lint.py", ev, self.root, "--bash-relint")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_bash_relint_catches_bypass_edit(self):
        unit = self.start()
        # simulate a sed/tee edit the Write hook never saw, including an
        # unregistered file
        (unit / "sneaky.md").write_text("x\n" + ACCT_TOKEN + "\n")
        ev = post_event(self.root, "Bash",
                        {"command": "tee units/u1/sneaky.md"})
        r = run_hook("posttool_lint.py", ev, self.root, "--bash-relint")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_malformed_stdin_fails_open(self):
        r = run_hook("posttool_lint.py", "{not json", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestStopGate(Base):
    def test_armed_incomplete_session_blocks_with_config_command(self):
        self.start(sid="S1")
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        # next-step text must come from adapter.yaml gate_commands, incl. slug
        self.assertIn("gate.py approve composed --unit u1", out["reason"])
        self.assertIn("pause", out["reason"])

    def test_unarmed_session_not_blocked(self):
        self.start(sid="S1")
        r = run_hook("stop_gate.py", stop_event(self.root, "S2"), self.root)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_paused_not_blocked(self):
        self.start(sid="S1")
        run_cli("gate.py", "pause", "--unit", "u1", root=self.root)
        self.consolidate_handoff("S1")
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        self.assertEqual(r.stdout.strip(), "")

    def test_complete_unit_not_blocked(self):
        unit = self.start(sid="S1")
        (unit / "note.md").write_text("done\n")
        run_cli("gate.py", "approve", "composed", "--unit", "u1", root=self.root)
        run_cli("gate.py", "approve", "checked", "--unit", "u1", root=self.root)
        r = run_cli("lint.py", "--unit", "u1", "--set-gate", "verified",
                    root=self.root)
        assert r.returncode == 0, r.stdout + r.stderr
        self.consolidate_handoff("S1")
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        self.assertEqual(r.stdout.strip(), "")

    def test_cap_allows_with_warning_after_three_blocks(self):
        self.start(sid="S1")
        blocks, allowed_with_warning = 0, False
        for _ in range(4):
            r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
            if r.stdout.strip() and json.loads(r.stdout).get("decision") == "block":
                blocks += 1
            elif "allowing stop" in (r.stdout + r.stderr).lower():
                allowed_with_warning = True
        self.assertEqual(blocks, 3)
        self.assertTrue(allowed_with_warning)


class TestSessionStart(Base):
    def test_reports_active_unit_and_next_command(self):
        self.start(sid="S1")
        ev = {"session_id": "S1", "cwd": str(self.root),
              "hook_event_name": "SessionStart"}
        r = run_hook("session_start.py", ev, self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("u1", r.stdout)
        self.assertIn("gate.py approve composed --unit u1", r.stdout)

    def test_quiet_without_active_unit(self):
        ev = {"session_id": "S1", "cwd": str(self.root),
              "hook_event_name": "SessionStart"}
        r = run_hook("session_start.py", ev, self.root)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
