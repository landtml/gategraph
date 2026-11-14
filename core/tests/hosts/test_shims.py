"""Shim behaviour (H1/H2, brief X1 part 2): the four hook shims driven the way Claude Code
drives them, a JSON event on stdin and CLAUDE_PROJECT_DIR set, against throwaway roots.

This replaces the legacy core/tests/test_hooks.py, which drove the deleted core/hooks/
scripts. The decisions themselves are covered in test_claude_code.py and test_session.py.

Run alone with: python3 core/tests/hosts/test_shims.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import json
import os
import subprocess
import tempfile
import time
import unittest

from core.hosts.dispatch import BUDGET_ENV
from core.kernel import paths

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from test_bridge import ACCT, make_root  # noqa: E402  (shared fixture builder)
from test_session import write_unit  # noqa: E402  (shared unit builder)

SHIMS = _bootstrap.REPO / "core" / "hosts" / "shims"


def run_shim(name, event, root, extra_env=None):
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root), **(extra_env or {})}
    text = event if isinstance(event, str) else json.dumps(event)
    return subprocess.run([sys.executable, str(SHIMS / name)], input=text,
                          capture_output=True, text=True, env=env, cwd=str(root), timeout=120)


def pre_write(sid, path, content):
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(path), "content": content}}


def post_write(sid, path):
    return {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": str(path), "content": ""},
            "tool_response": {"success": True}}


def pre_bash(sid, command):
    return {"session_id": sid, "hook_event_name": "PreToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}}


def post_bash(sid, command):
    return {"session_id": sid, "hook_event_name": "PostToolUse", "tool_name": "Bash",
            "tool_input": {"command": command}, "tool_response": {"stdout": "", "stderr": ""}}


def stop_event(sid):
    return {"session_id": sid, "hook_event_name": "Stop", "stop_hook_active": False}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_root(self._tmp.name)
        write_unit(self.root, "in_progress", [], slug="u1")
        (self.root / "units" / "u1" / "note.md").write_text("sketch\n", encoding="utf-8")

    @staticmethod
    def decision(proc):
        out = json.loads(proc.stdout)
        return out["hookSpecificOutput"]["permissionDecision"], \
            out["hookSpecificOutput"]["permissionDecisionReason"]


class TestPretoolGuard(Base):
    def test_deny_policy_content_denied(self):
        r = run_shim("pretool_guard.py",
                     pre_write("S1", self.root / "docs/x.md", "id: " + ACCT + "\n"), self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        decision, reason = self.decision(r)
        self.assertEqual(decision, "deny")
        self.assertIn("acct", reason.lower())

    def test_protected_path_denied(self):
        r = run_shim("pretool_guard.py",
                     pre_write("S1", self.root / "units/u1/manifest.yaml", "slug: u1\n"), self.root)
        decision, _ = self.decision(r)
        self.assertEqual(decision, "deny")

    def test_ask_path(self):
        r = run_shim("pretool_guard.py",
                     pre_write("S1", self.root / "templates/base.tex", "clean\n"), self.root)
        decision, _ = self.decision(r)
        self.assertEqual(decision, "ask")

    def test_clean_write_allowed_silently(self):
        r = run_shim("pretool_guard.py",
                     pre_write("S1", self.root / "units/u1/note.md", "a clean line\n"), self.root)
        self.assertEqual(r.returncode, 0)
        if r.stdout.strip():
            decision, _ = self.decision(r)
            self.assertNotEqual(decision, "deny")

    def test_malformed_stdin_fails_open(self):
        r = run_shim("pretool_guard.py", "{not json", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_unloadable_config_fails_closed_for_unit_writes(self):
        (self.root / "harness/adapter/adapter.yaml").write_text("{ corrupt: [\n")
        r = run_shim("pretool_guard.py",
                     pre_write("S1", self.root / "units/u1/note.md", "anything\n"), self.root)
        decision, reason = self.decision(r)
        self.assertEqual(decision, "deny")
        self.assertIn("adapter", reason.lower())


class TestPosttoolLint(Base):
    def test_fatal_finding_exit2(self):
        note = self.root / "units/u1/note.md"
        note.write_text("id " + ACCT + "\n", encoding="utf-8")
        r = run_shim("posttool_lint.py", post_write("S1", note), self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("BLOCKED", r.stderr)

    def test_write_in_active_unit_arms_marker(self):
        note = self.root / "units/u1/note.md"
        note.write_text("clean\n", encoding="utf-8")
        run_shim("posttool_lint.py", post_write("S9", note), self.root)
        self.assertTrue(paths.marker_path(self.root, "S9").exists())
        self.assertEqual(paths.marker_path(self.root, "S9"),
                         self.root / ".gategraph" / "touched-S9")

    def test_bash_bypass_is_caught_after_stamp(self):
        # a sed/tee edit the Write hook never saw: stamp at PreToolUse, then edit, then relint
        self.assertEqual(run_shim("pretool_guard.py", pre_bash("S1", "python3 build.py"), self.root).returncode, 0)
        (self.root / "units/u1/sneaky.md").write_text("x\n" + ACCT + "\n", encoding="utf-8")
        r = run_shim("posttool_lint.py", post_bash("S1", "python3 build.py"), self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_check_budget_env_bounds_the_run(self):
        note = self.root / "units/u1/note.md"
        note.write_text("hello\n", encoding="utf-8")
        t0 = time.monotonic()
        r = run_shim("posttool_lint.py", post_write("S1", note), self.root, {BUDGET_ENV: "1"})
        self.assertLess(time.monotonic() - t0, 10, "the host would have killed a slow hook")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_malformed_stdin_fails_open(self):
        r = run_shim("posttool_lint.py", "{not json", self.root)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestStopGate(Base):
    def test_armed_incomplete_session_blocks_with_config_command(self):
        paths.marker_path(self.root, "S1").parent.mkdir(parents=True, exist_ok=True)
        paths.marker_path(self.root, "S1").touch()
        r = run_shim("stop_gate.py", stop_event("S1"), self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("next gate is 'composed'", out["reason"])
        self.assertIn("pause", out["reason"])

    def test_unarmed_session_not_blocked(self):
        r = run_shim("stop_gate.py", stop_event("S2"), self.root)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_paused_not_blocked(self):
        write_unit(self.root, "paused", [], slug="u1")
        paths.marker_path(self.root, "S1").parent.mkdir(parents=True, exist_ok=True)
        paths.marker_path(self.root, "S1").touch()
        (self.root / "HANDOFF.md").write_text("## Now\nx\n\n## Next\ny\n", encoding="utf-8")
        r = run_shim("stop_gate.py", stop_event("S1"), self.root)
        self.assertEqual(r.stdout.strip(), "")

    def test_cap_allows_after_three_blocks(self):
        paths.marker_path(self.root, "S1").parent.mkdir(parents=True, exist_ok=True)
        paths.marker_path(self.root, "S1").touch()
        blocks = 0
        for _ in range(4):
            r = run_shim("stop_gate.py", stop_event("S1"), self.root)
            if r.stdout.strip() and json.loads(r.stdout).get("decision") == "block":
                blocks += 1
        self.assertEqual(blocks, 3)


class TestSessionStart(Base):
    def test_reports_active_unit_and_next_command(self):
        ev = {"session_id": "S1", "cwd": str(self.root), "hook_event_name": "SessionStart",
              "source": "startup"}
        r = run_shim("session_start.py", ev, self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("u1", r.stdout)

    def test_quiet_without_active_unit(self):
        (self.root / "units" / ".active").unlink()
        ev = {"session_id": "S1", "cwd": str(self.root), "hook_event_name": "SessionStart",
              "source": "startup"}
        r = run_shim("session_start.py", ev, self.root)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
